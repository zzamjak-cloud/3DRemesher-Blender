"""격리 프로필에서 LOOP 가이드가 QuadriFlow 경로의 절단 링이 되어 팔에 링 격자를 만드는지 검증한다.

합성 입력: 구(몸통) + ±x 축 원통(팔 둘)을 복셀로 합친 삼각 메시. +x 팔 둘레에 REMESH_GUIDE_ 닫힌 커브 하나를 둔다.
검사: 결과가 닫힌 쿼드 표면이고, 가이드 양쪽으로 닫힌 평행 링이 이어지며(나선이면 0), 원본이 바뀌지 않는다.
X 대칭에서는 양의 팔 루프가 절단 링이 되고 미러가 음의 팔에도 같은 링을 만들어야 한다.
대칭면을 가로지르는 루프(열린 호 접합)는 합성 Y 대칭 입력에서 대칭면 옆 QuadriFlow 미세 면 때문에 종횡비 검사에
걸려 여기서 검증하지 않는다.
"""

from __future__ import annotations

import importlib
import math
import time

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
quality = importlib.import_module(MODULE + ".addon.topology.quality")


def build_body():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, segments=48, ring_count=24)
    body = bpy.context.active_object
    arms = []
    for x in (1.6, -1.6):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.3, depth=2.6, vertices=48, location=(x, 0.0, 0.0), rotation=(0.0, math.pi / 2, 0.0))
        arms.append(bpy.context.active_object)
    body.select_set(True)
    for arm in arms:
        arm.select_set(True)
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    obj = bpy.context.active_object
    modifier = obj.modifiers.new("vox", "REMESH")
    modifier.mode = "VOXEL"
    modifier.voxel_size = 0.04
    bpy.ops.object.modifier_apply(modifier="vox")
    obj.modifiers.new("tri", "TRIANGULATE")
    bpy.ops.object.modifier_apply(modifier="tri")
    obj.name = "RingBody"
    return obj


def add_loop_guide(name, x, radius, count=24):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(count - 1)
    for index, point in enumerate(spline.points):
        angle = 2 * math.pi * index / count
        point.co = (x, radius * math.cos(angle), radius * math.sin(angle), 1.0)
    spline.use_cyclic_u = True
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def run_job(source, mode, target, symmetry_x):
    props = bpy.context.scene.zzamjak_3d_remesher
    props.topology_mode = mode
    props.target_quad_count = target
    props.symmetry_x = symmetry_x
    props.symmetry_y = False
    props.symmetry_z = False
    props.density_scale = 1.0
    before = operators._source_input_fingerprint(source, bpy.context.scene)
    engine_input, warnings = operators._build_engine_input(bpy.context, source)
    started = time.monotonic()
    job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
    try:
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


for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
source = build_body()
add_loop_guide("REMESH_GUIDE_arm", 1.3, 0.33)

result, seconds = run_job(source, "QUADRIFLOW", 1500, False)
q = result.quality
assert q.quad_ratio >= 0.99, q
assert q.non_manifold_edge_count == 0 and q.degenerate_face_count == 0 and q.boundary_edge_count == 0, q
assert q.max_aspect_ratio <= 20.0, q
seam = [w for w in result.warnings if "절단 링" in w]
assert seam, result.warnings
propagation = [w for w in result.warnings if "링 전파" in w]
assert propagation, result.warnings
print(f"[ring worker] QUADRIFLOW 1500 → {q.actual_quad_count}쿼드, 삼각형 {sum(1 for f in result.mesh.faces if len(f) == 3)}개, {seconds:.1f}초")
for w in result.warnings:
    if "LOOP" in w:
        print("[ring worker]", w)
# 접합부 양쪽에 닫힌 링(띠)이 있어야 하고, 팔 쪽(+x)으로는 닫힌 평행 링이 이어져야 한다.
# 몸통 쪽(−x)은 0.2 뒤에 구와 만나 링 수가 바뀌므로 전파 수는 요구하지 않는다.
for sign in (-1.0, 1.0):
    report = quality.ring_propagation(result.mesh, (1.3 + 0.13 * sign, 0.0, 0.0), (sign, 0.0, 0.0), 0.33, 3)
    assert report.belt_ok, (sign, report)
    if sign > 0:
        assert report.closed_rings >= 2, (sign, report)

# AUTO 도 격자 경로가 실패한 뒤 QuadriFlow 로 이어져 LOOP 를 보존해야 한다
result_auto, seconds_auto = run_job(source, "AUTO", 1500, False)
assert any("절단 링" in w for w in result_auto.warnings), result_auto.warnings
print(f"[ring worker] AUTO 1500 → {result_auto.quality.actual_quad_count}쿼드, {seconds_auto:.1f}초")

# 사용자가 팔보다 45% 큰 원을 그려도(반지름 0.435) 표면 링을 찾아 접합해야 한다
for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
add_loop_guide("REMESH_GUIDE_big", 1.3, 0.435)
result_big, seconds_big = run_job(source, "QUADRIFLOW", 1500, False)
assert any("절단 링" in w and "접합했습니다" in w for w in result_big.warnings), result_big.warnings
report = quality.ring_propagation(result_big.mesh, (1.43, 0.0, 0.0), (1.0, 0.0, 0.0), 0.33, 3)
assert report.belt_ok and report.closed_rings >= 2, report
print(f"[ring worker] QUADRIFLOW 큰 원(1.45배) 1500 → {result_big.quality.actual_quad_count}쿼드, {seconds_big:.1f}초")
for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
add_loop_guide("REMESH_GUIDE_arm", 1.3, 0.33)

# X 대칭: +x 팔의 루프가 절단 링이 되고, 미러가 −x 팔에도 같은 링 격자를 만들어야 한다
result_sym, seconds_sym = run_job(source, "QUADRIFLOW", 1500, True)
qs = result_sym.quality
assert qs.symmetry_error <= 1.0e-6 and qs.boundary_edge_count == 0 and qs.non_manifold_edge_count == 0, qs
assert any("절단 링" in w for w in result_sym.warnings), result_sym.warnings
for x, sign in ((1.43, 1.0), (-1.43, -1.0)):
    report = quality.ring_propagation(result_sym.mesh, (x, 0.0, 0.0), (sign, 0.0, 0.0), 0.33, 3)
    assert report.belt_ok and report.closed_rings >= 2, (x, report)
print(f"[ring worker] QUADRIFLOW X대칭 1500 → {qs.actual_quad_count}쿼드, 대칭 오차 {qs.symmetry_error:.2g}, {seconds_sym:.1f}초")
for w in result_sym.warnings:
    if "LOOP" in w:
        print("[ring worker]", w)

# X 대칭에서 루프를 −x 팔에만 그려도 양의 쪽으로 미러해 같은 결과가 나와야 한다 (조용히 무시되면 안 된다)
for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
add_loop_guide("REMESH_GUIDE_left_arm", -1.3, 0.33)
result_neg, seconds_neg = run_job(source, "QUADRIFLOW", 1500, True)
assert any("절단 링" in w for w in result_neg.warnings), result_neg.warnings
for x, sign in ((1.43, 1.0), (-1.43, -1.0)):
    report = quality.ring_propagation(result_neg.mesh, (x, 0.0, 0.0), (sign, 0.0, 0.0), 0.33, 3)
    assert report.belt_ok and report.closed_rings >= 2, (x, report)
print(f"[ring worker] QUADRIFLOW X대칭(−x 루프) 1500 → {result_neg.quality.actual_quad_count}쿼드, {seconds_neg:.1f}초")
print("[ring worker] OK")
