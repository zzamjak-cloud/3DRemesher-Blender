"""격리 Blender Extension worker에서 둥근 T 분기와 가이드 경로를 검증한다."""

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
from tests.test_rounded_branch import make_rounded_branch_guides, make_rounded_branch_source


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")
quality = importlib.import_module(MODULE + ".addon.topology.quality")


def _component_count(faces):
    neighbors = defaultdict(set)
    for face in faces:
        for a,b in zip(face,(*face[1:],face[0])):
            neighbors[a].add(b)
            neighbors[b].add(a)
    pending = set(neighbors)
    count = 0
    while pending:
        count += 1
        first = pending.pop()
        queue = deque((first,))
        while queue:
            for next_vertex in neighbors[queue.popleft()] & pending:
                pending.remove(next_vertex)
                queue.append(next_vertex)
    return count


source_data = make_rounded_branch_source()
guides = make_rounded_branch_guides()
mesh = bpy.data.meshes.new("둥근 T 삼각 입력")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("둥근 T 삼각 입력", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

for guide in guides:
    object_name = f"REMESH_GUIDE_{guide.kind[0]}_{guide.name}"
    curve = bpy.data.curves.new(object_name,"CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    points = guide.splines[0]
    spline.points.add(len(points)-1)
    for output_point,position in zip(spline.points,points):
        output_point.co = (*position,1.0)
    spline.use_cyclic_u = guide.closed[0]
    bpy.context.collection.objects.link(bpy.data.objects.new(object_name,curve))

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = 1072
before = operators._source_input_fingerprint(source,bpy.context.scene)
engine_input,warnings = operators._build_engine_input(bpy.context,source)
assert len(engine_input.guide_curves) == 4
job = operators._start_worker_job(source,bpy.context.scene,engine_input,warnings)
try:
    deadline = time.monotonic()+90
    while job.process.poll() is None:
        if time.monotonic()>deadline:
            raise AssertionError("둥근 T worker가 90초 안에 끝나지 않았습니다.")
        time.sleep(.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"),payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == 1072,result.quality
    assert result.quality.quad_ratio == 1.0
    assert result.quality.boundary_edge_count == 0
    assert result.quality.non_manifold_edge_count == 0
    assert _component_count(result.mesh.faces) == 1
    layout = quality.validate_layout(result.mesh,quality.LayoutExpectations(
        loops=tuple(quality.EdgePathExpectation(g.name,g.splines[0],True,.096) for g in guides[:3]),
        strips=(quality.EdgePathExpectation("arm_strip",guides[3].splines[0],False,.096),),
        boundary_edge_count=0,max_face_aspect_ratio=5.0,
    ))
    assert layout.ok,layout.issues
    assert quality.measure_bidirectional_sample_distance(source_data,result.mesh).max_distance < .08
    assert operators._source_input_fingerprint(source,bpy.context.scene) == before
    review = adapter.build_result_object(source,result.mesh)
    bpy.context.collection.objects.link(review)
    review.location.x += 3.5
    review.show_wire = True
    review.show_all_edges = True
    source.show_wire = True
    source.show_all_edges = True
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT/"dist"/"RoundedBranchReview.blend"))
    print("둥근 T Blender worker 검증 통과: 1072쿼드, 폐루프 3개·팔 방향선 1개, 원본 보존")
finally:
    operators._cleanup_job_files(job)
