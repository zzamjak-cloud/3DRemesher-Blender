from __future__ import annotations

import importlib

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
core = importlib.import_module(MODULE + ".addon.core")
adapter = importlib.import_module(MODULE + ".addon.blender_adapter")
large = importlib.import_module(MODULE + ".addon.large_mesh")

bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.subdivide(number_cuts=42)
bpy.ops.object.mode_set(mode="OBJECT")
assert len(source.data.polygons) > 10000
original_faces = tuple(tuple(face.vertices) for face in source.data.polygons)
engine_input = core.build_engine_input(
    adapter.mesh_data_from_object(source),
    core.RemeshSettings(target_quad_count=384, topology_mode="STRUCTURED"),
)
assert large.needs_preprocessing(engine_input.mesh)
result = large.remesh_large(engine_input)
assert result.quality.actual_quad_count == 384
used_vertices = {index for face in result.mesh.faces for index in face}
assert len(used_vertices) == len(result.mesh.vertices)
assert result.quality.non_manifold_edge_count == 0
assert result.quality.max_surface_error < 1e-6
assert tuple(tuple(face.vertices) for face in source.data.polygons) == original_faces
print("고밀도 큐브 Blender 검증 통과:", len(original_faces), "입력 면,", result.quality.actual_quad_count, "출력 쿼드")
