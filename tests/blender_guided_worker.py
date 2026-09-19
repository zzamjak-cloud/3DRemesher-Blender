from __future__ import annotations

import importlib
import math
import time

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")

segments = 16
heights = (0.0, 0.3, 1.7, 3.0)
vertices = [
    (math.cos(2 * math.pi * index / segments), math.sin(2 * math.pi * index / segments), height)
    for height in heights for index in range(segments)
]
faces = [
    (row * segments + index, row * segments + (index + 1) % segments,
     (row + 1) * segments + (index + 1) % segments, (row + 1) * segments + index)
    for row in range(len(heights) - 1) for index in range(segments)
]
mesh = bpy.data.meshes.new("가이드 worker 입력")
mesh.from_pydata(vertices, [], faces)
mesh.update()
source = bpy.data.objects.new("가이드 worker 입력", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

loop_curve = bpy.data.curves.new("REMESH_GUIDE_LOOP_limb", "CURVE")
loop_curve.dimensions = "3D"
loop = loop_curve.splines.new("POLY")
loop.points.add(31)
for index, point in enumerate(loop.points):
    angle = 2 * math.pi * index / 32
    point.co = (math.cos(angle), math.sin(angle), 1.4, 1.0)
loop.use_cyclic_u = True
bpy.context.collection.objects.link(bpy.data.objects.new(loop_curve.name, loop_curve))

strip_curve = bpy.data.curves.new("REMESH_GUIDE_STRIP_limb", "CURVE")
strip_curve.dimensions = "3D"
strip = strip_curve.splines.new("POLY")
strip.points.add(2)
for point, height in zip(strip.points, (0.0, 1.4, 3.0)):
    point.co = (1.0, 0.0, height, 1.0)
bpy.context.collection.objects.link(bpy.data.objects.new(strip_curve.name, strip_curve))

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = 128
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert {kind for guide in engine_input.guide_curves for kind in guide.kind} == {"LOOP", "STRIP"}
assert len(engine_input.density_values) == len(vertices)
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 30.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("가이드 worker가 30초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == 128
    assert sum(abs(point[2] - 1.4) < 1e-5 for point in result.mesh.vertices) == segments
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    print("가이드 worker 검증 통과: LOOP·STRIP 엣지, 128쿼드, 원본 보존")
finally:
    operators._cleanup_job_files(job)
