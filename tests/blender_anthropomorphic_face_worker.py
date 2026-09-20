"""격리 Blender Extension worker에서 합성 돌출형 얼굴의 여섯 루프를 검증한다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import time

import bpy


ROOT = Path(os.environ["REMESHER_DEV_ROOT"])
sys.path.insert(0, str(ROOT))
from tests.test_face_patch_anthropomorphic import _face_fixture


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")
quality = importlib.import_module(MODULE + ".addon.topology.quality")

startup_cube = bpy.data.objects.get("Cube")
if startup_cube is not None and startup_cube.type == "MESH":
    bpy.data.objects.remove(startup_cube, do_unlink=True)

source_data, guides = _face_fixture()
mesh = bpy.data.meshes.new("합성 얼굴 삼각 입력")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("합성 얼굴 삼각 입력", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

for guide in guides:
    curve_name = f"REMESH_GUIDE_LOOP_{guide.name}"
    curve = bpy.data.curves.new(curve_name, "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    points = guide.splines[0]
    spline.points.add(len(points) - 1)
    for output_point, position in zip(spline.points, points):
        output_point.co = (*position, 1.0)
    spline.use_cyclic_u = True
    bpy.context.collection.objects.link(bpy.data.objects.new(curve_name, curve))

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
            raise AssertionError("합성 얼굴 worker가 90초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert abs(result.quality.actual_quad_count - 5000) / 5000 <= 0.05
    assert result.quality.quad_ratio == 1.0
    assert result.quality.boundary_edge_count == 0
    assert result.quality.non_manifold_edge_count == 0
    loops = tuple(quality.EdgePathExpectation(guide.name, guide.splines[0], True, 0.06) for guide in guides)
    report = quality.validate_layout(result.mesh, quality.LayoutExpectations(
        loops=loops, boundary_edge_count=0, max_face_aspect_ratio=5.0,
    ))
    assert report.ok, report.issues
    assert report.metrics.bowtie_vertex_count == 0
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before

    review = adapter.build_result_object(source, result.mesh)
    bpy.context.collection.objects.link(review)
    review.location.x += 2.6
    review.show_wire = True
    review.show_all_edges = True
    source.show_wire = True
    source.show_all_edges = True
    bpy.ops.wm.save_as_mainfile(filepath=str(ROOT / "dist" / "AnthropomorphicFaceReview.blend"))
    print(
        f"합성 얼굴 Extension worker 검증 통과: {result.quality.actual_quad_count}쿼드, "
        f"최대 면 종횡비 {report.metrics.max_face_aspect_ratio:.3f}, "
        "여섯 LOOP 연속, 원본 지문 보존"
    )
finally:
    operators._cleanup_job_files(job)
