"""격리 Blender worker에서 연결형 T 표면의 세 폐루프를 검증한다."""

from __future__ import annotations

from collections import defaultdict, deque
import importlib
import os
from pathlib import Path
import sys
import time

import bpy


ROOT = Path(os.environ["REMESHER_DEV_ROOT"])
sys.path.insert(0, str(ROOT))
from tests.test_branch_t import _guides, _triangle_t


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")
quality = importlib.import_module(MODULE + ".addon.topology.quality")


def _component_count(faces: tuple[tuple[int, ...], ...]) -> int:
    adjacency = defaultdict(set)
    for face in faces:
        for first, second in zip(face, (*face[1:], face[0])):
            adjacency[first].add(second)
            adjacency[second].add(first)
    remaining = set(adjacency)
    count = 0
    while remaining:
        count += 1
        first = min(remaining)
        remaining.remove(first)
        pending = deque((first,))
        while pending:
            for neighbor in adjacency[pending.popleft()] & remaining:
                remaining.remove(neighbor)
                pending.append(neighbor)
    return count


source_data = _triangle_t(3, alternate=True)
guides = _guides()
mesh = bpy.data.meshes.new("T형 삼각 입력")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("T형 삼각 입력", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

for guide in guides:
    curve = bpy.data.curves.new(guide.name, "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    points = guide.splines[0]
    spline.points.add(len(points) - 1)
    for output_point, position in zip(spline.points, points):
        output_point.co = (*position, 1.0)
    spline.use_cyclic_u = True
    bpy.context.collection.objects.link(bpy.data.objects.new(guide.name, curve))

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = 900
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert len(engine_input.guide_curves) == 3
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 90.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("T형 worker가 90초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == 928, result.quality
    assert result.quality.quad_ratio == 1.0
    assert result.quality.boundary_edge_count == 0
    assert result.quality.non_manifold_edge_count == 0
    assert _component_count(result.mesh.faces) == 1
    loops = tuple(quality.EdgePathExpectation(guide.name, guide.splines[0], closed=True, tolerance=0.04)
                  for guide in guides)
    layout = quality.validate_layout(result.mesh, quality.LayoutExpectations(
        loops=loops, boundary_edge_count=0, max_face_aspect_ratio=5.0,
    ))
    assert layout.ok, layout.issues
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    review = adapter.build_result_object(source, result.mesh)
    bpy.context.collection.objects.link(review)
    review.location.x += 3.5
    review.show_wire = True
    review.show_all_edges = True
    source.show_wire = True
    source.show_all_edges = True
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "dist" / "BranchTReview.blend"))
    print("T형 Blender worker 검증 통과: 928쿼드, 닫힌 단일 컴포넌트, 몸통·팔 폐루프 3개, 원본 보존")
finally:
    operators._cleanup_job_files(job)
