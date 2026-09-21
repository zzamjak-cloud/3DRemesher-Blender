"""격리 프로필에서 QuadriFlow 경로의 실제 worker 실행·AUTO 라우팅·원본 보존을 검증한다."""

from __future__ import annotations

import importlib
import time
from collections import Counter

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")


def run_job(source, mode: str, target: int, symmetry_x: bool):
    props = bpy.context.scene.zzamjak_3d_remesher
    props.topology_mode = mode
    props.target_quad_count = target
    props.symmetry_x = symmetry_x
    props.symmetry_y = False  # 같은 세션의 앞선 검사가 남긴 대칭 설정을 이어받지 않는다
    props.symmetry_z = False
    props.density_scale = 1.0
    before = operators._source_input_fingerprint(source, bpy.context.scene)
    engine_input, warnings = operators._build_engine_input(bpy.context, source)
    started = time.monotonic()
    job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
    try:
        assert "--factory-startup" in job.process.args, "QuadriFlow 경로는 Blender 바이너리 worker 로 실행돼야 합니다."
        assert "--large" not in job.process.args
        while job.process.poll() is None:
            if time.monotonic() - started > 300:
                raise AssertionError(f"{mode} worker 가 300초 안에 끝나지 않았습니다.")
            time.sleep(0.1)
        payload = operators._read_job_result(job)
        assert payload.get("ok"), payload
        assert before == operators._source_input_fingerprint(source, bpy.context.scene), "원본이 바뀌었습니다."
        return operators._deserialize_remesh_result(payload["result"]), time.monotonic() - started
    finally:
        operators._cleanup_job_files(job)


# 같은 세션의 앞선 검사가 남긴 가이드 커브가 있으면 AUTO 가 실험 엔진을 고르므로 먼저 지운다.
for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
bpy.ops.mesh.primitive_monkey_add()
source = bpy.context.active_object
source_faces = len(source.data.polygons)

# 1) QUADRIFLOW 모드 + X 대칭
result, seconds = run_job(source, "QUADRIFLOW", 2000, True)
quality = result.quality
assert quality.quad_ratio == 1.0, quality
assert quality.non_manifold_edge_count == 0 and quality.degenerate_face_count == 0, quality
assert quality.boundary_edge_count == 0, quality
assert quality.symmetry_error <= 1.0e-6, quality
assert 0.5 * 2000 <= quality.actual_quad_count <= 1.5 * 2000, quality
assert quality.max_aspect_ratio <= 20.0, quality
assert any("QuadriFlow 경로" in warning for warning in result.warnings), result.warnings
mirrored = Counter(tuple(round(c, 5) for c in v) for v in result.mesh.vertices)
assert all((round(-x, 5), y, z) in mirrored for (x, y, z) in mirrored), "미러된 정점 쌍이 없습니다."
print(f"QUADRIFLOW: 원본 {source_faces}면 → {quality.actual_quad_count}쿼드, 최대 종횡비 {quality.max_aspect_ratio:.2f}, "
      f"대칭 오차 {quality.symmetry_error:.2g}, {seconds:.1f}초")

# 2) AUTO 모드: 격자 전략이 다루지 못하는 형상은 QuadriFlow 로 이어지고 실험 엔진으로 내려가지 않는다
result, seconds = run_job(source, "AUTO", 1500, False)
assert result.quality.quad_ratio == 1.0, result.quality
assert result.quality.non_manifold_edge_count == 0, result.quality
assert any("QuadriFlow 경로" in warning for warning in result.warnings), result.warnings
assert not any("실험 엔진" in warning for warning in result.warnings), result.warnings
print(f"AUTO: {result.quality.actual_quad_count}쿼드, 최대 종횡비 {result.quality.max_aspect_ratio:.2f}, {seconds:.1f}초")

# 3) 취소: QuadriFlow 자식 프로세스까지 함께 끝나고 작업 폴더가 지워진다
import subprocess

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "QUADRIFLOW"
props.target_quad_count = 20000
before_cancel = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
cancel_job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    fraction, message = operators._read_job_progress(cancel_job)
    if "QuadriFlow 실행" in message or cancel_job.process.poll() is not None:
        break
    time.sleep(0.05)
assert cancel_job.process.poll() is None, "취소 검사 전에 worker 가 끝났습니다."
time.sleep(1.0)  # 자식 QuadriFlow 프로세스가 뜰 시간


def quadriflow_children() -> list[str]:
    """QuadriFlow 자식 Blender 프로세스 PID. 이 검사 스크립트 자신(tests/blender_quadriflow_worker.py)은 제외한다."""
    import os

    found = subprocess.run(["pgrep", "-f", "addon/quadriflow_worker.py"], capture_output=True, text=True).stdout.split()
    return [pid for pid in found if pid != str(os.getpid())]


grandchildren = quadriflow_children()
assert grandchildren, "취소 검사 시점에 QuadriFlow 자식 프로세스가 없습니다."
operators._request_job_cancel(cancel_job, terminate=True)
operators._cleanup_job_files(cancel_job)
assert cancel_job.process.poll() is not None
assert not cancel_job.temp_dir.exists()
deadline = time.monotonic() + 15.0
while True:
    survivors = quadriflow_children()
    if not survivors or time.monotonic() > deadline:
        break
    time.sleep(0.2)  # 종료 신호를 받은 Blender 가 내려가는 데 시간이 걸릴 수 있다
assert not survivors, f"취소 뒤 QuadriFlow 자식 프로세스가 남았습니다: {survivors}"
assert before_cancel == operators._source_input_fingerprint(source, bpy.context.scene)
print("취소: worker·자식 프로세스 종료, 작업 폴더 삭제 확인")

# 4) 결과 오브젝트 생성 경로 (원본 보존, 머티리얼·변환 승계)
result_obj = adapter.build_result_object(source, result.mesh, name_suffix="_QuadriFlow")
adapter.link_result_object(bpy.context, source, result_obj)
assert result_obj.name in bpy.data.objects and source.name in bpy.data.objects
assert len(source.data.polygons) == source_faces
print("QuadriFlow worker 검증 통과")
