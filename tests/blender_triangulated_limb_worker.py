"""격리 Blender worker에서 삼각 팔 옆면의 새 링 격자를 검증한다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import time

import bpy


sys.path.insert(0, str(Path(os.environ["REMESHER_DEV_ROOT"])))
from tests.test_triangulated_limb_integration import _guides, _tube


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")
quality = importlib.import_module(MODULE + ".addon.topology.quality")

source_data = _tube(13, 8, alternating=False)
guides = _guides()
mesh = bpy.data.meshes.new("삼각 팔 옆면")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("삼각 팔 옆면", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

for guide in guides:
    curve = bpy.data.curves.new(guide.name, "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(len(guide.splines[0]) - 1)
    for output_point, position in zip(spline.points, guide.splines[0]):
        output_point.co = (*position, 1.0)
    spline.use_cyclic_u = guide.kind[0] == "LOOP"
    bpy.context.collection.objects.link(bpy.data.objects.new(guide.name, curve))

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = 256
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert len(engine_input.guide_curves) == 2
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 60.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("삼각 팔 worker가 60초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == 256
    assert result.quality.quad_ratio == 1.0
    assert result.quality.non_manifold_edge_count == 0
    report = quality.validate_layout(result.mesh, quality.LayoutExpectations(
        loops=(quality.EdgePathExpectation(guides[0].name, guides[0].splines[0], closed=True, tolerance=0.13),),
        strips=(quality.EdgePathExpectation(guides[1].name, guides[1].splines[0], closed=False,
                                            tolerance=0.13, endpoints_on_boundary=True),),
        max_face_aspect_ratio=5.0,
    ))
    assert report.ok, report.issues
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    print("삼각 팔 worker 검증 통과: 256쿼드, 관절 링·길이 방향 열, 원본 보존")
finally:
    operators._cleanup_job_files(job)
