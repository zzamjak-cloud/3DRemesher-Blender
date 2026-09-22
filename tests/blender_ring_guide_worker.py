"""격리 프로필에서 클릭 링 가이드 생성 → 둘레 비율 → 오프셋 재슬라이스 → QuadriFlow 절단 링 접합까지 확인한다."""

from __future__ import annotations

import importlib
import math
import time

import bpy

MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
ring_guide = importlib.import_module(MODULE + ".addon.ring_guide")
worker_helpers = open(__file__.replace("blender_ring_guide_worker.py", "blender_quadriflow_ring_worker.py"), encoding="utf-8").read()
exec(worker_helpers.split("def run_job")[0].split("operators = importlib")[0].replace('MODULE = "bl_ext.user_default.zzamjak_3d_remesher"', ""))
exec("def build_body" + worker_helpers.split("def build_body", 1)[1].split("def add_loop_guide")[0])
exec("def run_job" + worker_helpers.split("def run_job", 1)[1].split("\nfor stray in")[0])

for stray in [obj for obj in bpy.data.objects if obj.name.startswith("REMESH_GUIDE_")]:
    bpy.data.objects.remove(stray, do_unlink=True)
source = build_body()
source.location = (0.3, 0.2, 0.0)  # 월드·로컬 변환이 섞이지 않는지 확인
bpy.context.view_layer.update()
props = bpy.context.scene.zzamjak_3d_remesher
props.target_quad_count = 1500

# 1) 팔 옆면(로컬) 클릭 → 반지름 0.3 링, 축 x, 둘레 비율 ≈ 1
guide, reason = ring_guide.create_ring_guide(bpy.context, source, (1.5, 0.0, 0.3), (0.0, 0.0, 1.0))
assert guide is not None, reason
g = guide.zzamjak_ring_guide
assert g.is_ring and g.source == source
assert abs(abs(g.axis[0]) - 1.0) < 0.05, tuple(g.axis)
assert abs(g.radius - 0.3) < 0.03, g.radius
assert g.ratio > 0.9, (g.ratio, g.status)
spline = guide.data.splines[0]
assert spline.use_cyclic_u and len(spline.points) == 32
xs = [p.co.x for p in spline.points]
assert all(abs(x - 1.5) < 0.02 for x in xs), (min(xs), max(xs))
assert guide.matrix_world.translation == source.matrix_world.translation
print(f"[ring guide] 생성: 반지름 {g.radius:.3f}, 축 {tuple(round(a, 2) for a in g.axis)}, 비율 {g.ratio:.2f}, {g.status}")

# 2) 오프셋으로 팔 끝쪽 0.4 이동 → 커브가 따라가고 여전히 사용 가능
g.offset = 0.4 if g.axis[0] > 0 else -0.4
ring_guide.refresh_ring_guide(guide)  # GUI 에서는 update 콜백이 타이머로 미뤄 호출한다 — 백그라운드에는 이벤트 루프가 없다
xs = [p.co.x for p in guide.data.splines[0].points]
assert all(abs(x - 1.9) < 0.03 for x in xs), (min(xs), max(xs))
assert g.ratio > 0.9, g.ratio
g.offset = 0.0
ring_guide.refresh_ring_guide(guide)

# 3) 팔 뿌리(구와 만나는 곳) 클릭도 닫힌 단면을 만들고 비율을 보고한다 (값은 형상에 따라 다르므로 검사하지 않음)
root, reason = ring_guide.create_ring_guide(bpy.context, source, (1.02, 0.0, 0.3), (0.0, 0.0, 1.0))
assert root is not None, reason
print(f"[ring guide] 팔 뿌리: 비율 {root.zzamjak_ring_guide.ratio:.2f}, {root.zzamjak_ring_guide.status}")
bpy.data.objects.remove(root, do_unlink=True)

# 4) 삭제 오퍼레이터: 이름으로 하나 지우기, 전체 지우기 뒤 다시 만들기
extra, reason = ring_guide.create_ring_guide(bpy.context, source, (1.9, 0.0, 0.3), (0.0, 0.0, 1.0))
assert extra is not None, reason
assert len(ring_guide.ring_guides(bpy.context.scene)) == 2
assert bpy.ops.object.zzamjak_3d_remesher_remove_ring_guide(name=extra.name) == {"FINISHED"}
assert len(ring_guide.ring_guides(bpy.context.scene)) == 1
assert bpy.ops.object.zzamjak_3d_remesher_clear_ring_guides() == {"FINISHED"}
assert not ring_guide.ring_guides(bpy.context.scene) and not [o for o in bpy.data.objects if o.name.startswith("REMESH_GUIDE_")]
guide, reason = ring_guide.create_ring_guide(bpy.context, source, (1.5, 0.0, 0.3), (0.0, 0.0, 1.0))
assert guide is not None, reason
print("[ring guide] 삭제·전체 삭제·재생성 OK")

# 5) 검사 오퍼레이터가 등록·실행되고, 만든 가이드로 QuadriFlow 절단 링이 접합된다
bpy.ops.object.select_all(action="DESELECT")
source.select_set(True)
bpy.context.view_layer.objects.active = source
assert bpy.ops.object.zzamjak_3d_remesher_check_ring_guides() == {"FINISHED"}
result, seconds = run_job(source, "QUADRIFLOW", 1500, False)
assert any("절단 링" in w and "접합했습니다" in w for w in result.warnings), result.warnings
print(f"[ring guide] QUADRIFLOW 1500 → {result.quality.actual_quad_count}쿼드, {seconds:.1f}초")
print("[ring guide] OK")
