from __future__ import annotations

from dataclasses import dataclass
from array import array
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import subprocess
import sys
import tempfile

import bpy

from .blender_adapter import (
    build_result_object,
    collect_guide_curves,
    density_values_from_mesh,
    ensure_density_attribute,
    format_input_report,
    format_result_report,
    link_result_object,
    mesh_data_from_object,
    result_mesh_data,
    settings_from_scene,
    unsupported_control_warnings,
)
from .core import MeshData, RemeshBackend


@dataclass
class _RunJob:
    source_name: str
    source_pointer: int
    source_mesh_pointer: int
    source_geometry_fingerprint: tuple
    scene_pointer: int
    warnings: tuple[str, ...]
    temp_dir: Path
    input_path: Path
    result_path: Path
    progress_path: Path
    cancel_path: Path
    stderr_path: Path
    process: subprocess.Popen | None = None
    timer: object | None = None


_ACTIVE_JOB: _RunJob | None = None


def _active_mesh_object(context):
    obj = context.object
    if obj is None or obj.type != "MESH":
        return None
    return obj


def _require_object_mode(operator, context, action: str) -> bool:
    if context.mode == "OBJECT":
        return True
    operator.report({"ERROR"}, f"{action}은 오브젝트 모드에서 실행해야 합니다.")
    return False


def _build_engine_input(context, obj):
    settings = settings_from_scene(context.scene)
    warnings = list(unsupported_control_warnings(context.scene))
    if obj.modifiers:
        warnings.append("모디파이어는 적용하지 않고 원본 로컬 메시 데이터를 사용했습니다.")
    backend = RemeshBackend()
    density_values = density_values_from_mesh(obj.data, settings.density_attribute_name, 1.0)
    engine_input = backend.build_input(
        mesh_data_from_object(obj),
        settings,
        collect_guide_curves(context.scene, obj),
        density_values,
    )
    return engine_input, tuple(warnings)


def _set_progress(props, fraction: float, message: str) -> None:
    props.run_progress = max(0.0, min(1.0, float(fraction)))
    props.run_progress_message = message


def _report_lines(operator, level, message: str) -> None:
    lines = [line.strip() for line in str(message).splitlines() if line.strip()]
    if not lines:
        return
    first, *rest = lines
    operator.report(level, first)
    for line in rest:
        operator.report({"INFO"}, line)


def _is_cancelled_exception(exc: BaseException) -> bool:
    return type(exc).__name__ == "RemeshCancelled"


def _remesh_with_backend(engine_input):
    def progress(fraction, message=""):
        return None

    def cancelled():
        return False

    from .large_mesh import needs_preprocessing, remesh_large

    if needs_preprocessing(engine_input.mesh):
        return remesh_large(engine_input, progress=progress, cancelled=cancelled)
    return RemeshBackend().remesh(engine_input, progress=progress, cancelled=cancelled)


def _clear_active_job(job: _RunJob | None = None) -> None:
    global _ACTIVE_JOB
    if job is None or _ACTIVE_JOB is job:
        _ACTIVE_JOB = None


def cancel_active_job() -> None:
    global _ACTIVE_JOB
    job = _ACTIVE_JOB
    if job is not None:
        _request_job_cancel(job, terminate=True)
        if job.timer is not None:
            try:
                bpy.context.window_manager.event_timer_remove(job.timer)
            except Exception:
                pass
        _cleanup_job_files(job)
        _clear_active_job(job)


class ZJREMESH_OT_analyze(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_analyze"
    bl_label = "메시 분석"
    bl_description = "선택한 메시를 적응형 리메시 엔진 입력으로 변환하고 품질 지표를 계산합니다"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "메시 분석"):
            return {"CANCELLED"}

        try:
            engine_input, warnings = _build_engine_input(context, obj)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        message = format_input_report(engine_input, warnings)
        context.scene.zzamjak_3d_remesher.last_report = message
        _report_lines(self, {"WARNING"} if warnings else {"INFO"}, message)
        return {"FINISHED"}


class ZJREMESH_OT_prepare_density(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_prepare_density"
    bl_label = "밀도 속성 준비"
    bl_description = "선택한 메시의 점 도메인 FLOAT_COLOR 밀도 속성을 준비합니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "밀도 속성 준비"):
            return {"CANCELLED"}
        if obj.data.users > 1:
            self.report({"ERROR"}, "공유 메시 데이터입니다. 단일 사용자 메시로 만든 뒤 실행하세요.")
            return {"CANCELLED"}

        props = context.scene.zzamjak_3d_remesher
        try:
            attribute = ensure_density_attribute(obj.data, props.density_attribute_name)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        message = f"밀도 속성 준비 완료: {attribute.name}"
        props.last_report = message
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ZJREMESH_OT_run(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_run"
    bl_label = "리메시 실행"
    bl_description = "적응형 리메시 엔진으로 별도 리메시 오브젝트를 생성합니다"
    bl_options = {"REGISTER", "UNDO"}

    _job = None

    @classmethod
    def unregister(cls):
        cancel_active_job()

    def invoke(self, context, event):
        if bpy.app.background or context.window is None:
            return self.execute(context)
        if _ACTIVE_JOB is not None:
            self.report({"WARNING"}, "이미 리메시를 실행 중입니다.")
            return {"CANCELLED"}
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "리메시 실행"):
            return {"CANCELLED"}

        props = context.scene.zzamjak_3d_remesher
        try:
            engine_input, warnings = _build_engine_input(context, obj)
        except ValueError as exc:
            props.last_report = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        try:
            job = _start_worker_job(obj, context.scene, engine_input, warnings)
        except OSError as exc:
            props.last_report = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        try:
            job.timer = context.window_manager.event_timer_add(0.1, window=context.window)
            self._job = job
            self._set_active_job(job)
            props.run_busy = True
            _set_progress(props, 0.0, "리메시 시작")
            context.window_manager.modal_handler_add(self)
        except Exception:
            if job.timer is not None:
                try:
                    context.window_manager.event_timer_remove(job.timer)
                except Exception:
                    pass
            _cleanup_job_files(job)
            _clear_active_job(job)
            props.run_busy = False
            _set_progress(props, 0.0, "대기 중")
            raise
        _redraw_context(context)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        job = self._job
        if job is None:
            return {"CANCELLED"}
        props = _job_props(job, context)

        if event.type == "ESC":
            _request_job_cancel(job, terminate=True)
            _set_progress(props, props.run_progress, "취소 요청 중")
            _redraw_context(context)
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        progress_fraction, progress_message = _read_job_progress(job)
        _set_progress(props, progress_fraction, progress_message or "실행 중")
        _redraw_context(context)

        if job.process is not None and job.process.poll() is None:
            return {"RUNNING_MODAL"}

        return self._finish_modal(context, job)

    def execute(self, context):
        if _ACTIVE_JOB is not None:
            self.report({"WARNING"}, "이미 리메시를 실행 중입니다.")
            return {"CANCELLED"}
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "리메시 실행"):
            return {"CANCELLED"}

        props = context.scene.zzamjak_3d_remesher
        props.run_busy = True
        _set_progress(props, 0.0, "리메시 시작")
        try:
            engine_input, warnings = _build_engine_input(context, obj)
            remesh_result = _remesh_with_backend(engine_input)
            return self._finish_success(context, obj, remesh_result, warnings)
        except NotImplementedError as exc:
            props.last_report = str(exc)
            _report_lines(self, {"WARNING"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            if _is_cancelled_exception(exc):
                props.last_report = "리메시를 취소했습니다."
                self.report({"WARNING"}, props.last_report)
            else:
                props.last_report = str(exc)
                self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            props.run_busy = False
            _set_progress(props, 0.0, "대기 중")

    def _finish_modal(self, context, job: _RunJob):
        props = _job_props(job, context)
        try:
            if job.timer is not None:
                context.window_manager.event_timer_remove(job.timer)
                job.timer = None
            if job.cancel_path.exists():
                props.last_report = "리메시를 취소했습니다."
                self.report({"WARNING"}, props.last_report)
                return {"CANCELLED"}
            if _scene_by_pointer(job.scene_pointer) is not context.scene:
                props.last_report = "씬이 바뀌어 결과를 만들지 않았습니다."
                self.report({"ERROR"}, props.last_report)
                return {"CANCELLED"}
            source_obj = _object_by_pointer(job.source_pointer)
            if source_obj is None or source_obj.type != "MESH":
                props.last_report = "원본 메시 오브젝트를 찾을 수 없어 결과를 만들지 않았습니다."
                self.report({"ERROR"}, props.last_report)
                return {"CANCELLED"}
            if source_obj.data.as_pointer() != job.source_mesh_pointer or _source_input_fingerprint(source_obj, context.scene) != job.source_geometry_fingerprint:
                props.last_report = "원본 입력이 실행 중 변경되어 결과를 만들지 않았습니다."
                self.report({"ERROR"}, props.last_report)
                return {"CANCELLED"}
            result_payload = _read_job_result(job)
            if not result_payload.get("ok"):
                error_type = result_payload.get("error_type", "RuntimeError")
                message = result_payload.get("message", "리메시 작업이 실패했습니다.")
                if error_type == "RemeshCancelled":
                    props.last_report = "리메시를 취소했습니다."
                    self.report({"WARNING"}, props.last_report)
                elif error_type == "NotImplementedError":
                    props.last_report = message
                    _report_lines(self, {"WARNING"}, props.last_report)
                else:
                    props.last_report = message
                    self.report({"ERROR"}, message)
                return {"CANCELLED"}
            try:
                remesh_result = _deserialize_remesh_result(result_payload["result"])
                return self._finish_success(context, source_obj, remesh_result, job.warnings)
            except Exception as exc:
                props.last_report = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
        except Exception as exc:
            props.last_report = f"리메시 결과 처리 중 오류가 발생했습니다: {exc}"
            self.report({"ERROR"}, props.last_report)
            return {"CANCELLED"}
        finally:
            props.run_busy = False
            _set_progress(props, 0.0, "대기 중")
            _cleanup_job_files(job)
            _clear_active_job(job)

    def _finish_success(self, context, source_obj, remesh_result, warnings: tuple[str, ...]):
        mesh_data = result_mesh_data(remesh_result)
        result_obj = build_result_object(source_obj, mesh_data)
        selection_snapshot = tuple(context.selected_objects)
        active_object = context.view_layer.objects.active
        try:
            link_result_object(context, source_obj, result_obj)
        except Exception:
            _remove_unlinked_result(result_obj)
            for selected in context.selected_objects:
                selected.select_set(False)
            for selected in selection_snapshot:
                if selected.name in bpy.data.objects:
                    selected.select_set(True)
            if active_object is not None and active_object.name in bpy.data.objects:
                context.view_layer.objects.active = active_object
            raise
        message = format_result_report(remesh_result, warnings)
        context.scene.zzamjak_3d_remesher.last_report = message
        _report_lines(self, {"WARNING"} if "알림:" in message else {"INFO"}, message)
        return {"FINISHED"}

    def _set_active_job(self, job: _RunJob) -> None:
        global _ACTIVE_JOB
        _ACTIVE_JOB = job


def _object_by_pointer(pointer: int):
    for obj in bpy.data.objects:
        if obj.as_pointer() == pointer:
            return obj
    return None


def _scene_by_pointer(pointer: int):
    for scene in bpy.data.scenes:
        if scene.as_pointer() == pointer:
            return scene
    return None


def _job_props(job: _RunJob, context):
    scene = _scene_by_pointer(job.scene_pointer)
    if scene is None:
        scene = context.scene
    return scene.zzamjak_3d_remesher


def _remove_unlinked_result(result_obj) -> None:
    mesh = result_obj.data
    bpy.data.objects.remove(result_obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _start_worker_job(source_obj, scene, engine_input, warnings: tuple[str, ...]) -> _RunJob:
    from .large_mesh import needs_preprocessing

    large_input = needs_preprocessing(engine_input.mesh)
    executable = Path(bpy.app.binary_path) if large_input else _python_executable()
    if executable is None or not executable.is_file():
        raise OSError("Blender 번들 Python 실행 파일을 찾을 수 없습니다.")

    temp_dir = Path(tempfile.mkdtemp(prefix="zzamjak_3d_remesh_"))
    job = _RunJob(
        source_name=source_obj.name,
        source_pointer=source_obj.as_pointer(),
        source_mesh_pointer=source_obj.data.as_pointer(),
        source_geometry_fingerprint=_source_input_fingerprint(source_obj, scene),
        scene_pointer=scene.as_pointer(),
        warnings=warnings,
        temp_dir=temp_dir,
        input_path=temp_dir / "input.json",
        result_path=temp_dir / "result.json",
        progress_path=temp_dir / "progress.json",
        cancel_path=temp_dir / "cancel",
        stderr_path=temp_dir / "worker.log",
    )
    try:
        _write_json(job.input_path, _engine_input_payload(engine_input))
        worker_path = Path(__file__).with_name("worker.py")
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(repo_root), env.get("PYTHONPATH", "")) if item
        )
        arguments = [str(job.input_path), str(job.result_path), str(job.progress_path), str(job.cancel_path)]
        if large_input:
            profile = temp_dir / "profile"
            for kind, relative in (
                ("RESOURCES", ""), ("CONFIG", "config"), ("SCRIPTS", "scripts"),
                ("DATAFILES", "datafiles"), ("EXTENSIONS", "extensions"),
            ):
                directory = profile / relative
                directory.mkdir(parents=True, exist_ok=True)
                env[f"BLENDER_USER_{kind}"] = str(directory)
            command = [
                str(executable), "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
                "--python-exit-code", "1", "--python", str(worker_path), "--", "--large", *arguments,
            ]
        else:
            command = [str(executable), str(worker_path), *arguments]
        stderr_handle = job.stderr_path.open("wb")
    except Exception:
        _cleanup_job_files(job)
        raise
    try:
        job.process = subprocess.Popen(
            command,
            cwd=str(repo_root),
            env=env,
            stdout=stderr_handle,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        stderr_handle.close()
        _cleanup_job_files(job)
        raise
    stderr_handle.close()
    return job


def _python_executable() -> Path | None:
    candidates = [Path(sys.executable)]
    versioned = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates.extend(
        [
            Path(sys.prefix) / "bin" / versioned,
            Path(sys.prefix) / "bin" / "python3",
            Path(sys.prefix) / "bin" / "python",
        ]
    )
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _request_job_cancel(job: _RunJob, *, terminate: bool = False) -> None:
    try:
        job.cancel_path.write_text("cancel", encoding="utf-8")
    except Exception:
        pass
    if terminate and job.process is not None and job.process.poll() is None:
        job.process.terminate()


def _read_job_progress(job: _RunJob) -> tuple[float, str]:
    if not job.progress_path.exists():
        return 0.0, "실행 중"
    try:
        payload = json.loads(job.progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0, "실행 중"
    return float(payload.get("fraction", 0.0)), str(payload.get("message", "실행 중"))


def _read_job_result(job: _RunJob) -> dict:
    if job.result_path.exists():
        return json.loads(job.result_path.read_text(encoding="utf-8"))
    return_code = job.process.returncode if job.process is not None else None
    log_text = ""
    if job.stderr_path.exists():
        log_text = job.stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    if job.cancel_path.exists():
        return {"ok": False, "error_type": "RemeshCancelled", "message": "리메시를 취소했습니다."}
    message = log_text or f"리메시 작업이 결과 파일 없이 종료되었습니다. 종료 코드: {return_code}"
    return {"ok": False, "error_type": "RuntimeError", "message": message}


def _cleanup_job_files(job: _RunJob) -> None:
    if job.process is not None and job.process.poll() is None:
        job.process.terminate()
        try:
            job.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            job.process.kill()
            job.process.wait(timeout=1.0)
    shutil.rmtree(job.temp_dir, ignore_errors=True)


def _engine_input_payload(engine_input) -> dict:
    return {
        "mesh": _mesh_payload(engine_input.mesh),
        "settings": {
            "target_quad_count": engine_input.settings.target_quad_count,
            "symmetry_axes": list(engine_input.settings.symmetry_axes),
            "hard_edge_angle_degrees": engine_input.settings.hard_edge_angle_degrees,
            "guide_curve_names": list(engine_input.settings.guide_curve_names),
            "density_attribute_name": engine_input.settings.density_attribute_name,
            "density_scale": engine_input.settings.density_scale,
            "topology_mode": engine_input.settings.topology_mode,
        },
        "guide_curves": [
            {
                "name": guide.name,
                "splines": [[list(point) for point in spline] for spline in guide.splines],
                "kind": list(guide.kind),
                "closed": list(guide.closed),
            }
            for guide in engine_input.guide_curves
        ],
        "density_values": list(engine_input.density_values),
    }


def _mesh_payload(mesh: MeshData) -> dict:
    return {
        "vertices": [list(vertex) for vertex in mesh.vertices],
        "faces": [list(face) for face in mesh.faces],
        "hard_edges": [list(edge) for edge in sorted(mesh.hard_edges)],
    }


def _deserialize_remesh_result(payload: dict):
    mesh = MeshData(
        vertices=tuple(tuple(float(component) for component in vertex) for vertex in payload["mesh"]["vertices"]),
        faces=tuple(tuple(int(index) for index in face) for face in payload["mesh"]["faces"]),
        hard_edges=frozenset(tuple(int(index) for index in edge) for edge in payload["mesh"].get("hard_edges", ())),
    )
    quality_payload = payload.get("quality")
    quality = SimpleNamespace(**quality_payload) if quality_payload else None
    return SimpleNamespace(
        mesh=mesh,
        quality=quality,
        warnings=tuple(payload.get("warnings", ())),
        unsupported_controls=tuple(payload.get("unsupported_controls", ())),
    )


def _write_json(path: Path, payload: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, path)


def _source_geometry_fingerprint(obj) -> str:
    mesh = obj.data
    digest = hashlib.sha256()
    # 큰 입력의 변경 감지에 좌표·면 튜플을 한 벌 더 보관하지 않는다.
    for collection, attribute, stride, typecode in (
        (mesh.vertices, "co", 3, "f"),
        (mesh.loops, "vertex_index", 1, "i"),
        (mesh.polygons, "loop_total", 1, "i"),
        (mesh.edges, "vertices", 2, "i"),
    ):
        values = array(typecode, [0]) * (len(collection) * stride)
        collection.foreach_get(attribute, values)
        digest.update(len(values).to_bytes(8, "little"))
        digest.update(values.tobytes())
    digest.update(bytes(int(edge.use_seam) | (int(getattr(edge, "use_edge_sharp", False)) << 1) for edge in mesh.edges))
    return digest.hexdigest()


def _source_input_fingerprint(obj, scene) -> tuple:
    props = scene.zzamjak_3d_remesher
    density_values = density_values_from_mesh(obj.data, props.density_attribute_name, 1.0)
    guides = collect_guide_curves(scene, obj)
    return (
        _source_geometry_fingerprint(obj),
        props.density_attribute_name,
        float(props.density_scale),
        tuple(float(value) for value in density_values),
        tuple((guide.name, guide.splines, guide.kind, guide.closed) for guide in guides),
        getattr(props, "topology_mode", "AUTO"),
        bool(props.symmetry_x),
        bool(props.symmetry_y),
        bool(props.symmetry_z),
        float(props.hard_edge_angle),
        int(props.target_quad_count),
    )


def _redraw_context(context) -> None:
    area = getattr(context, "area", None)
    if area is not None:
        area.tag_redraw()


CLASSES = (
    ZJREMESH_OT_analyze,
    ZJREMESH_OT_prepare_density,
    ZJREMESH_OT_run,
)
