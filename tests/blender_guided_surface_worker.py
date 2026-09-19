"""격리 Blender에서 삼각 얼굴 격자의 가이드 폐루프 복원과 worker 경로를 검증한다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import time

import bpy

sys.path.insert(0, str(Path(os.environ["REMESHER_DEV_ROOT"])))
from tests.test_guided_surface import _face_guides, _rounded_head_mesh, _triangulated


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
quality = importlib.import_module(MODULE + ".addon.topology.quality")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")

quad_mesh, by_coord = _rounded_head_mesh(12)
source_mesh = _triangulated(quad_mesh)
guides = _face_guides(quad_mesh, by_coord)

mesh = bpy.data.meshes.new("얼굴 가이드 검증 입력")
mesh.from_pydata(source_mesh.vertices, [], source_mesh.faces)
mesh.update()
source = bpy.data.objects.new("얼굴 가이드 검증 입력", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

for guide in guides:
    guide_name = f"REMESH_GUIDE_LOOP_{guide.name}"
    curve = bpy.data.curves.new(guide_name, "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    points = guide.splines[0]
    if points[0] == points[-1]:
        points = points[:-1]
    spline.points.add(len(points) - 1)
    for output_point, position in zip(spline.points, points):
        output_point.co = (*position, 1.0)
    spline.use_cyclic_u = True
    bpy.context.collection.objects.link(bpy.data.objects.new(guide_name, curve))

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = len(quad_mesh.faces)
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert len(engine_input.guide_curves) == 3
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 60.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("얼굴 가이드 worker가 60초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == len(quad_mesh.faces)
    assert result.quality.max_aspect_ratio < 2.0
    assert result.quality.non_manifold_edge_count == 0
    loops = tuple(
        quality.EdgePathExpectation(guide.name, guide.splines[0], closed=True, tolerance=0.01)
        for guide in guides
    )
    report = quality.validate_layout(result.mesh, quality.LayoutExpectations(loops=loops))
    assert report.ok, report.issues
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    review = adapter.build_result_object(source, result.mesh)
    bpy.context.collection.objects.link(review)
    review.location.x += 2.5
    review.show_wire = True
    review.show_all_edges = True
    source.show_wire = True
    source.show_all_edges = True
    review_path = Path(os.environ["REMESHER_DEV_ROOT"]) / "dist" / "GuidedFaceReview.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(review_path))
    print("삼각 얼굴 worker 검증 통과: 3개 폐루프, 3456쿼드, 원본 보존")
finally:
    operators._cleanup_job_files(job)
