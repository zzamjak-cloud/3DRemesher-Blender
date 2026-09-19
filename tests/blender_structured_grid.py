from __future__ import annotations

import importlib
import math
import os
from collections import Counter
from pathlib import Path

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
core = importlib.import_module(MODULE + ".addon.core")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
source.name = "구조 격자 검증 큐브"
original = tuple(tuple(vertex.co) for vertex in source.data.vertices)

settings = core.RemeshSettings(target_quad_count=384, topology_mode="STRUCTURED")
engine_input = core.build_engine_input(adapter.mesh_data_from_object(source), settings)
result = core.RemeshBackend().remesh(engine_input)
output = adapter.build_result_object(source, result.mesh)
bpy.context.collection.objects.link(output)

assert tuple(tuple(vertex.co) for vertex in source.data.vertices) == original
assert len(output.data.polygons) == 384
assert all(len(face.vertices) == 4 for face in output.data.polygons)
degrees = Counter()
edge_counts = Counter()
for face in output.data.polygons:
    vertices = tuple(face.vertices)
    for index, vertex in enumerate(vertices):
        edge_counts[tuple(sorted((vertex, vertices[(index + 1) % 4])))] += 1
for first, second in edge_counts:
    degrees[first] += 1
    degrees[second] += 1
assert all(count == 2 for count in edge_counts.values())
assert Counter(degrees.values()) == {3: 8, 4: len(output.data.vertices) - 8}
assert result.quality.max_surface_error < 1e-6
print("구조 격자 Blender 검증 통과:", len(output.data.vertices), "정점,", len(output.data.polygons), "쿼드")

segments = 16
heights = (0.0, 0.3, 1.7, 3.0)
tube_vertices = [
    (math.cos(2 * math.pi * segment / segments), math.sin(2 * math.pi * segment / segments), height)
    for height in heights
    for segment in range(segments)
]
tube_faces = [
    (
        row * segments + segment,
        row * segments + (segment + 1) % segments,
        (row + 1) * segments + (segment + 1) % segments,
        (row + 1) * segments + segment,
    )
    for row in range(len(heights) - 1)
    for segment in range(segments)
]
tube_mesh = bpy.data.meshes.new("구조 격자 검증 원통")
tube_mesh.from_pydata(tube_vertices, [], tube_faces)
tube_mesh.update()
tube_obj = bpy.data.objects.new("구조 격자 검증 원통", tube_mesh)
bpy.context.collection.objects.link(tube_obj)
tube_input = core.build_engine_input(
    adapter.mesh_data_from_object(tube_obj),
    core.RemeshSettings(target_quad_count=128, topology_mode="STRUCTURED"),
)
tube_result = core.RemeshBackend().remesh(tube_input)
assert len(tube_result.mesh.faces) == 128
assert tube_result.quality.boundary_edge_count == 32
assert tube_result.quality.non_manifold_edge_count == 0
ring_rows = len(tube_result.mesh.vertices) // segments
assert ring_rows == 9
tube_edges = {
    tuple(sorted((face[index], face[(index + 1) % 4])))
    for face in tube_result.mesh.faces
    for index in range(4)
}
for row in range(ring_rows):
    for segment in range(segments):
        first = row * segments + segment
        next_segment = row * segments + (segment + 1) % segments
        assert tuple(sorted((first, next_segment))) in tube_edges
        if row < ring_rows - 1:
            assert tuple(sorted((first, (row + 1) * segments + segment))) in tube_edges
print("주기 격자 Blender 검증 통과:", ring_rows, "개 링,", len(tube_result.mesh.faces), "쿼드")

loop_curve = bpy.data.curves.new("REMESH_GUIDE_LOOP_limb", "CURVE")
loop_curve.dimensions = "3D"
loop_spline = loop_curve.splines.new("POLY")
loop_spline.points.add(31)
for index, point in enumerate(loop_spline.points):
    angle = 2 * math.pi * index / 32
    point.co = (math.cos(angle), math.sin(angle), 1.4, 1.0)
loop_spline.use_cyclic_u = True
loop_obj = bpy.data.objects.new(loop_curve.name, loop_curve)
bpy.context.collection.objects.link(loop_obj)

strip_curve = bpy.data.curves.new("REMESH_GUIDE_STRIP_limb", "CURVE")
strip_curve.dimensions = "3D"
strip_spline = strip_curve.splines.new("POLY")
strip_spline.points.add(2)
for point, height in zip(strip_spline.points, (0.0, 1.4, 3.0)):
    point.co = (1.0, 0.0, height, 1.0)
strip_obj = bpy.data.objects.new(strip_curve.name, strip_curve)
bpy.context.collection.objects.link(strip_obj)

guides = adapter.collect_guide_curves(bpy.context.scene, tube_obj)
assert {kind for guide in guides for kind in guide.kind} == {"LOOP", "STRIP"}
guided_input = core.build_engine_input(
    adapter.mesh_data_from_object(tube_obj),
    core.RemeshSettings(target_quad_count=128, topology_mode="STRUCTURED"),
    guide_curves=guides,
)
guided_result = core.RemeshBackend().remesh(guided_input)
assert guided_result.quality.actual_quad_count == 128
assert sum(abs(point[2] - 1.4) < 1e-5 for point in guided_result.mesh.vertices) == 16
print("가이드 Blender 검증 통과: LOOP 링 + STRIP 세로 열")

review_path = os.environ.get("REMESHER_GRID_REVIEW")
if review_path:
    tube_output = adapter.build_result_object(tube_obj, tube_result.mesh)
    bpy.context.collection.objects.link(tube_output)
    output.location.x = -2.5
    tube_output.location.x = 2.5
    source.hide_set(True)
    tube_obj.hide_set(True)
    for review_obj in (output, tube_output):
        review_obj.display_type = "WIRE"
        review_obj.show_in_front = True
    destination = Path(review_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    print("검토용 Blend 저장:", destination)
