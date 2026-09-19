"""격리 Blender worker에서 새 얼굴 루프 격자 생성과 원본 보존을 검증한다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import time

import bpy


sys.path.insert(0, str(Path(os.environ["REMESHER_DEV_ROOT"])))
from tests.test_face_patch_integration import _extended_face_guides, _sphere_mesh


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")
quality = importlib.import_module(MODULE + ".addon.topology.quality")

source_data = _sphere_mesh(31, 17, alternating=False)
guides = _extended_face_guides()
mesh = bpy.data.meshes.new("독립 삼각 얼굴 입력")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("독립 삼각 얼굴 입력", mesh)
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
props.target_quad_count = 5000
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert len(engine_input.guide_curves) == 6
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 90.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("새 얼굴 격자 worker가 90초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert abs(result.quality.actual_quad_count - 5000) / 5000 <= 0.05
    assert result.quality.quad_ratio == 1.0
    assert result.quality.non_manifold_edge_count == 0
    loops = tuple(quality.EdgePathExpectation(guide.name, guide.splines[0], closed=True, tolerance=0.06)
                  for guide in guides)
    layout = quality.validate_layout(result.mesh, quality.LayoutExpectations(loops=loops, max_face_aspect_ratio=5.0))
    assert layout.ok, layout.issues
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    review = adapter.build_result_object(source, result.mesh)
    bpy.context.collection.objects.link(review)
    review.location.x += 2.5
    review.show_wire = True
    review.show_all_edges = True
    source.show_wire = True
    source.show_all_edges = True
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(os.environ["REMESHER_DEV_ROOT"]) / "dist" / "NewFacePatchReview.blend"))
    print("새 얼굴 격자 worker 검증 통과: 입력 삼각 연결과 독립된 6개 폐루프, 원본 보존")
finally:
    operators._cleanup_job_files(job)
