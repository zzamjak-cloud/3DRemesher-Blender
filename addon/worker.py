from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings


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
        engine_input = _build_engine_input(payload)

        def progress(fraction, message=""):
            _write_json(progress_path, {"fraction": float(fraction), "message": str(message or "")})

        def cancelled():
            return cancel_path.exists()

        progress(0.0, "엔진 준비")
        if large_input:
            import bpy
            from addon.large_mesh import remesh_large

            bpy.context.preferences.filepaths.temporary_directory = str(input_path.parent) + os.sep
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


def _build_engine_input(payload: dict):
    mesh_payload = payload["mesh"]
    mesh = MeshData(
        vertices=tuple(tuple(float(component) for component in vertex) for vertex in mesh_payload["vertices"]),
        faces=tuple(tuple(int(index) for index in face) for face in mesh_payload["faces"]),
        hard_edges=frozenset(tuple(int(index) for index in edge) for edge in mesh_payload.get("hard_edges", ())),
    )
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
    density_values = tuple(float(value) for value in payload.get("density_values", ()))
    return RemeshBackend().build_input(mesh, settings, guides, density_values)


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
