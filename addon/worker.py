from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings

_GEOMETRY_PROTOCOL_VERSION = 1
_GEOMETRY_BUFFER_MAGIC = b"ZJREMESH_GEOMETRY\0"
_MAX_GEOMETRY_BUFFER_SIZE = 2 * 1024 * 1024 * 1024
_PACK_UINT32 = struct.Struct("<I")
_PACK_VECTOR3 = struct.Struct("<ddd")
_PACK_FLOAT64 = struct.Struct("<d")


def main(argv: list[str]) -> int:
    large_input = len(argv) > 1 and argv[1] == "--large"
    if large_input:
        argv = [argv[0], *argv[2:]]
    if len(argv) != 5:
        print("usage: worker.py input.json result.json progress.json cancel", file=sys.stderr)
        return 2

    input_path = Path(argv[1])
    result_path = Path(argv[2])
    progress_path = Path(argv[3])
    cancel_path = Path(argv[4])

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        engine_input = _build_engine_input(payload, input_path.parent)

        def progress(fraction, message=""):
            _write_json(progress_path, {"fraction": float(fraction), "message": str(message or "")})

        def cancelled():
            return cancel_path.exists()

        progress(0.0, "엔진 준비")
        _configure_blender_temp(input_path.parent)
        _install_terminate_handler()
        if large_input:
            from addon.large_mesh import remesh_large

            result = remesh_large(engine_input, progress=progress, cancelled=cancelled)
        else:
            result = RemeshBackend().remesh(engine_input, progress=progress, cancelled=cancelled)
        _write_json(result_path, {"ok": True, "result": _serialize_result(result)})
        return 0
    except BaseException as exc:
        _write_json(
            result_path,
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        return 1


def _configure_blender_temp(directory: Path) -> None:
    """Blender 바이너리로 실행된 worker 라면 Blender 임시 파일과 QuadriFlow 중간 .blend 를 작업 폴더에 쓰게 한다.

    작업 폴더는 부모가 정리하므로 취소·강제 종료 뒤에도 파일이 남지 않는다. 번들 Python 이면 아무것도 하지 않는다."""
    try:
        import bpy
    except ModuleNotFoundError:
        return
    from addon import quadriflow_path

    bpy.context.preferences.filepaths.temporary_directory = str(directory) + os.sep
    quadriflow_path.set_temp_root(directory)


def _install_terminate_handler() -> None:
    """부모의 SIGTERM 을 협조적 취소로 바꿔 진행 중인 QuadriFlow 자식 프로세스까지 정리하게 한다."""
    import signal

    from addon.core import RemeshCancelled

    def handler(signum, frame):
        raise RemeshCancelled("부모 프로세스가 작업을 중단했습니다.")

    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, AttributeError):
        pass


def _build_engine_input(payload: dict, input_dir: Path | str | None = None):
    if "geometry_buffer" in payload:
        _verify_input_fingerprint(payload)
        mesh, density_values = _read_geometry_buffer(payload, input_dir)
    else:
        mesh_payload = payload["mesh"]
        mesh = MeshData(
            vertices=tuple(tuple(float(component) for component in vertex) for vertex in mesh_payload["vertices"]),
            faces=tuple(tuple(int(index) for index in face) for face in mesh_payload["faces"]),
            hard_edges=frozenset(tuple(int(index) for index in edge) for edge in mesh_payload.get("hard_edges", ())),
        )
        density_values = tuple(float(value) for value in payload.get("density_values", ()))
    settings_payload = payload["settings"]
    settings = RemeshSettings(
        target_quad_count=int(settings_payload["target_quad_count"]),
        symmetry_axes=tuple(settings_payload.get("symmetry_axes", ())),
        hard_edge_angle_degrees=float(settings_payload["hard_edge_angle_degrees"]),
        guide_curve_names=tuple(settings_payload.get("guide_curve_names", ())),
        density_attribute_name=str(settings_payload["density_attribute_name"]),
        density_scale=float(settings_payload["density_scale"]),
        topology_mode=str(settings_payload.get("topology_mode", "AUTO")),
    )
    guides = tuple(
        GuideCurveData(
            name=str(guide["name"]),
            splines=tuple(
                tuple(tuple(float(component) for component in point) for point in spline)
                for spline in guide.get("splines", ())
            ),
            kind=tuple(str(value) for value in guide.get("kind", ())),
            closed=tuple(bool(value) for value in guide.get("closed", ())),
        )
        for guide in payload.get("guide_curves", ())
    )
    return RemeshBackend().build_input(mesh, settings, guides, density_values)


def _read_geometry_buffer(payload: dict, input_dir: Path | str | None) -> tuple[MeshData, tuple[float, ...]]:
    protocol_version = int(payload.get("protocol_version", 0))
    if protocol_version != _GEOMETRY_PROTOCOL_VERSION:
        raise ValueError(f"지원하지 않는 worker 입력 프로토콜입니다: {protocol_version}")
    if input_dir is None:
        raise ValueError("바이너리 geometry buffer를 읽으려면 입력 디렉터리가 필요합니다.")

    buffer_payload = payload["geometry_buffer"]
    buffer_path = _resolve_buffer_path(Path(input_dir), str(buffer_payload["filename"]))
    expected_size = int(buffer_payload["size"])
    expected_sha256 = str(buffer_payload["sha256"])
    _verify_buffer_file(buffer_path, expected_size, expected_sha256)

    sections = buffer_payload.get("sections", {})
    if buffer_payload.get("byte_order") != "little":
        raise ValueError("geometry buffer byte_order는 little이어야 합니다.")
    _validate_sections(sections, expected_size)
    with buffer_path.open("rb") as handle:
        magic = _read_exact(handle, len(_GEOMETRY_BUFFER_MAGIC))
        if magic != _GEOMETRY_BUFFER_MAGIC:
            raise ValueError("geometry buffer 헤더가 올바르지 않습니다.")
        version = _read_uint32(handle)
        if version != _GEOMETRY_PROTOCOL_VERSION:
            raise ValueError(f"geometry buffer 버전을 지원하지 않습니다: {version}")
        vertices = _read_vertices(handle, sections["vertices"])
        faces = _read_faces(handle, sections["faces"])
        hard_edges = _read_hard_edges(handle, sections["hard_edges"])
        density_values = _read_density_values(handle, sections["density_values"])
    return MeshData(vertices=vertices, faces=faces, hard_edges=hard_edges), density_values


def _resolve_buffer_path(input_dir: Path, filename: str) -> Path:
    relative = Path(filename)
    if relative.is_absolute() or relative.name != filename:
        raise ValueError("geometry buffer는 입력 temp dir 안의 파일명만 사용할 수 있습니다.")
    base_dir = input_dir.resolve()
    path = (base_dir / filename).resolve()
    if path.parent != base_dir:
        raise ValueError("geometry buffer 경로가 입력 temp dir 밖을 가리킵니다.")
    return path


def _verify_buffer_file(path: Path, expected_size: int, expected_sha256: str) -> None:
    if expected_size < 0 or expected_size > _MAX_GEOMETRY_BUFFER_SIZE:
        raise ValueError(f"geometry buffer 크기가 상한을 벗어났습니다: {expected_size}")
    stat = path.stat()
    if stat.st_size > _MAX_GEOMETRY_BUFFER_SIZE:
        raise ValueError(f"geometry buffer 실제 크기가 상한을 벗어났습니다: {stat.st_size}")
    if stat.st_size != expected_size:
        raise ValueError(f"geometry buffer 크기가 다릅니다: expected={expected_size}, actual={stat.st_size}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("geometry buffer SHA-256 검증에 실패했습니다.")


def _verify_input_fingerprint(payload: dict) -> None:
    expected = str(payload.get("input_fingerprint", ""))
    if not expected:
        raise ValueError("worker 입력 fingerprint가 없습니다.")
    actual = _payload_fingerprint(payload)
    if actual != expected:
        raise ValueError("worker 입력 fingerprint 검증에 실패했습니다.")


def _payload_fingerprint(payload: dict) -> str:
    fingerprint_payload = dict(payload)
    fingerprint_payload.pop("input_fingerprint", None)
    data = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _validate_sections(sections: dict, buffer_size: int) -> None:
    header_size = len(_GEOMETRY_BUFFER_MAGIC) + _PACK_UINT32.size
    ranges = []
    specs = (
        ("vertices", "float64", 3, _PACK_FLOAT64.size),
        ("faces", "uint32_varlen", None, _PACK_UINT32.size),
        ("hard_edges", "uint32", 2, _PACK_UINT32.size),
        ("density_values", "float64", 1, _PACK_FLOAT64.size),
    )
    for name, dtype, components, item_size in specs:
        section = _section_payload(sections, name, dtype=dtype, components=components)
        offset = int(section["offset"])
        count = int(section["count"])
        if offset < header_size or count < 0:
            raise ValueError(f"geometry buffer {name} 섹션 메타데이터가 잘못되었습니다.")
        if name == "faces":
            index_count = int(section.get("index_count", -1))
            if index_count < 0:
                raise ValueError("geometry buffer faces 섹션 index_count가 잘못되었습니다.")
            byte_count = (count + index_count) * item_size
        else:
            byte_count = count * int(section.get("components", 1)) * item_size
        end = offset + byte_count
        if end > buffer_size:
            raise ValueError(f"geometry buffer {name} 섹션이 파일 크기를 벗어났습니다.")
        ranges.append((offset, end, name))
    for previous, current in zip(sorted(ranges), sorted(ranges)[1:]):
        if current[0] < previous[1]:
            raise ValueError(f"geometry buffer 섹션 범위가 겹칩니다: {previous[2]}, {current[2]}")


def _section_payload(sections: dict, name: str, *, dtype: str, components: int | None = None) -> dict:
    if name not in sections:
        raise ValueError(f"geometry buffer {name} 섹션이 없습니다.")
    section = sections[name]
    if section.get("dtype") != dtype:
        raise ValueError(f"geometry buffer {name} 섹션 dtype이 다릅니다.")
    if components is not None and int(section.get("components", -1)) != components:
        raise ValueError(f"geometry buffer {name} 섹션 components가 다릅니다.")
    return section


def _read_vertices(handle, section: dict) -> tuple[tuple[float, float, float], ...]:
    _seek_section(handle, section)
    vertices = tuple(_PACK_VECTOR3.unpack(_read_exact(handle, _PACK_VECTOR3.size)) for _ in range(int(section["count"])))
    _verify_section_end(handle, section, int(section["count"]) * int(section["components"]) * _PACK_FLOAT64.size, "vertices")
    return vertices


def _read_faces(handle, section: dict) -> tuple[tuple[int, ...], ...]:
    _seek_section(handle, section)
    faces = []
    total_indices = 0
    for _ in range(int(section["count"])):
        count = _read_uint32(handle)
        total_indices += count
        faces.append(tuple(_read_uint32(handle) for _index in range(count)))
    if total_indices != int(section.get("index_count", total_indices)):
        raise ValueError("geometry buffer 면 인덱스 개수가 메타데이터와 다릅니다.")
    _verify_section_end(handle, section, (int(section["count"]) + total_indices) * _PACK_UINT32.size, "faces")
    return tuple(faces)


def _read_hard_edges(handle, section: dict) -> frozenset[tuple[int, int]]:
    _seek_section(handle, section)
    hard_edges = frozenset((_read_uint32(handle), _read_uint32(handle)) for _ in range(int(section["count"])))
    _verify_section_end(handle, section, int(section["count"]) * int(section["components"]) * _PACK_UINT32.size, "hard_edges")
    return hard_edges


def _read_density_values(handle, section: dict) -> tuple[float, ...]:
    _seek_section(handle, section)
    values = tuple(_PACK_FLOAT64.unpack(_read_exact(handle, _PACK_FLOAT64.size))[0] for _ in range(int(section["count"])))
    _verify_section_end(handle, section, int(section["count"]) * int(section["components"]) * _PACK_FLOAT64.size, "density_values")
    return values


def _seek_section(handle, section: dict) -> None:
    offset = int(section["offset"])
    if offset < 0:
        raise ValueError("geometry buffer 섹션 offset이 잘못되었습니다.")
    handle.seek(offset)


def _verify_section_end(handle, section: dict, byte_count: int, name: str) -> None:
    expected_end = int(section["offset"]) + byte_count
    if handle.tell() != expected_end:
        raise ValueError(f"geometry buffer {name} 섹션 길이가 메타데이터와 다릅니다.")


def _read_uint32(handle) -> int:
    return _PACK_UINT32.unpack(_read_exact(handle, _PACK_UINT32.size))[0]


def _read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("geometry buffer가 예상보다 짧습니다.")
    return data


def _serialize_result(result) -> dict:
    mesh = getattr(result, "mesh", result)
    quality = getattr(result, "quality", None)
    return {
        "mesh": _serialize_mesh(mesh),
        "quality": _serialize_object(quality) if quality is not None else None,
        "warnings": list(getattr(result, "warnings", ())),
        "unsupported_controls": list(getattr(result, "unsupported_controls", ())),
    }


def _serialize_mesh(mesh: MeshData) -> dict:
    return {
        "vertices": [list(vertex) for vertex in mesh.vertices],
        "faces": [list(face) for face in mesh.faces],
        "hard_edges": [list(edge) for edge in sorted(mesh.hard_edges)],
    }


def _serialize_object(value) -> dict:
    if is_dataclass(value):
        raw = asdict(value)
    elif isinstance(value, SimpleNamespace):
        raw = vars(value)
    else:
        raw = {
            name: getattr(value, name)
            for name in (
                "target_quad_count",
                "actual_quad_count",
                "target_error_ratio",
                "quad_ratio",
                "boundary_edge_count",
                "non_manifold_edge_count",
                "degenerate_face_count",
                "max_aspect_ratio",
                "mean_aspect_ratio",
                "max_surface_error",
                "mean_surface_error",
                "symmetry_error",
                "field_alignment",
            )
            if hasattr(value, name)
        }
    return {key: _json_value(item) for key, item in raw.items()}


def _json_value(value):
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_json_value(item) for item in sorted(value)]
    return value


def _write_json(path: Path, payload: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    raise SystemExit(main([__file__, *arguments]))
