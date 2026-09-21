from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import bpy


ADDON_MODULE = "bl_ext.user_default.zzamjak_3d_remesher"


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _make_quad(name: str):
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def _wait_process(process, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.05)
    raise AssertionError("worker subprocess가 시간 안에 종료되지 않았습니다.")


class _DummyOperator:
    def report(self, _level, _message):
        return None


def main():
    importlib.import_module(ADDON_MODULE)
    operators = importlib.import_module(f"{ADDON_MODULE}.addon.operators")

    obj = _make_quad("WorkerSource")
    bpy.context.scene.zzamjak_3d_remesher.target_quad_count = 64
    engine_input, warnings = operators._build_engine_input(bpy.context, obj)
    job = operators._start_worker_job(obj, bpy.context.scene, engine_input, warnings)
    try:
        _wait_process(job.process)
        payload = operators._read_job_result(job)
        assert_true("ok" in payload, "worker 결과 JSON 형식이 아닙니다.")
        assert_true(payload["ok"], f"worker 성공 결과가 아닙니다: {payload.get('message')}")
        assert_true(job.process.returncode == 0, "worker 성공 결과의 종료 코드가 0이 아닙니다.")
        result = operators._deserialize_remesh_result(payload["result"])
        assert_true(result.mesh.faces, "worker 성공 결과 메시가 비어 있습니다.")
        assert_true(all(len(face) == 4 for face in result.mesh.faces), "worker 결과에 쿼드가 아닌 면이 있습니다.")
        assert_true(result.quality.actual_quad_count == len(result.mesh.faces), "worker 품질 실제 쿼드 수가 결과 면 수와 다릅니다.")
    finally:
        temp_dir = job.temp_dir
        operators._cleanup_job_files(job)
    assert_true(not temp_dir.exists(), "worker 임시 디렉터리가 정리되지 않았습니다.")

    cancel_obj = _make_quad("WorkerCancelSource")
    bpy.context.scene.zzamjak_3d_remesher.target_quad_count = 64
    engine_input, warnings = operators._build_engine_input(bpy.context, cancel_obj)
    cancel_job = operators._start_worker_job(cancel_obj, bpy.context.scene, engine_input, warnings)
    temp_dir = cancel_job.temp_dir
    operators._request_job_cancel(cancel_job, terminate=True)
    operators._cleanup_job_files(cancel_job)
    assert_true(not temp_dir.exists(), "취소된 worker 임시 디렉터리가 정리되지 않았습니다.")

    bad_temp_dir = Path(os.environ["REMESHER_DEV_PROFILE"]) / f"bad_result_job_{os.getpid()}"
    bad_temp_dir.mkdir(exist_ok=True)
    bad_job = operators._RunJob(
        source_name=obj.name,
        source_pointer=obj.as_pointer(),
        source_mesh_pointer=obj.data.as_pointer(),
        source_geometry_fingerprint=operators._source_input_fingerprint(obj, bpy.context.scene),
        scene_pointer=bpy.context.scene.as_pointer(),
        warnings=(),
        temp_dir=bad_temp_dir,
        input_path=bad_temp_dir / "input.json",
        result_path=bad_temp_dir / "result.json",
        progress_path=bad_temp_dir / "progress.json",
        cancel_path=bad_temp_dir / "cancel",
        stderr_path=bad_temp_dir / "worker.log",
    )
    bad_job.result_path.write_text("{", encoding="utf-8")
    props = bpy.context.scene.zzamjak_3d_remesher
    props.run_busy = True
    result = operators.ZJREMESH_OT_run._finish_modal(_DummyOperator(), bpy.context, bad_job)
    assert_true(result == {"CANCELLED"}, "깨진 worker 결과가 취소로 처리되지 않았습니다.")
    assert_true("리메시 결과 처리 중 오류" in props.last_report, "깨진 worker 결과 오류가 보고되지 않았습니다.")
    assert_true(not props.run_busy, "깨진 worker 결과 처리 후 busy 상태가 남았습니다.")
    assert_true(not bad_temp_dir.exists(), "깨진 worker 결과 임시 디렉터리가 정리되지 않았습니다.")

    fake_result = operators._deserialize_remesh_result(
        {
            "mesh": {
                "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
                "faces": [[0, 1, 2, 3]],
                "hard_edges": [[0, 1]],
            },
            "quality": {
                "target_quad_count": 4,
                "actual_quad_count": 1,
                "target_error_ratio": 0.75,
                "quad_ratio": 1.0,
                "boundary_edge_count": 4,
                "non_manifold_edge_count": 0,
                "degenerate_face_count": 0,
                "max_aspect_ratio": 1.0,
                "mean_aspect_ratio": 1.0,
                "max_surface_error": 0.0,
                "mean_surface_error": 0.0,
                "symmetry_error": 0.0,
                "field_alignment": 1.0,
            },
            "warnings": ["테스트"],
            "unsupported_controls": ["density"],
        }
    )
    assert_true(fake_result.mesh.hard_edges == frozenset({(0, 1)}), "결과 hard edge 역직렬화 실패")
    assert_true(fake_result.quality.actual_quad_count == 1, "품질 지표 역직렬화 실패")
    print("Blender engine worker test passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Blender engine worker test failed: {exc}", file=sys.stderr)
        raise
