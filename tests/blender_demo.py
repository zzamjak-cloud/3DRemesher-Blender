"""전용 Blender 프로세스에서 원본과 쿼드 결과를 비교할 데모를 만든다."""

from pathlib import Path

import bpy
from mathutils import Vector


def main():
    scene = bpy.data.scenes.new("3D Remesher 적응형 엔진")
    bpy.context.window.scene = scene
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1.5, location=(-2.0, 0.0, 0.0))
    source = bpy.context.object
    source.name = "원본 — 삼각형 1280개"
    source.show_wire = True
    source.show_all_edges = True
    original_vertices = tuple(tuple(vertex.co) for vertex in source.data.vertices)
    original_faces = tuple(tuple(face.vertices) for face in source.data.polygons)
    props = scene.zzamjak_3d_remesher
    props.symmetry_x = False
    props.symmetry_y = False
    props.symmetry_z = False
    props.target_quad_count = 256
    result = bpy.ops.object.zzamjak_3d_remesher_run()
    assert result == {"FINISHED"}, result
    output = bpy.context.object
    assert output is not source and output.data is not source.data
    assert all(len(face.vertices) == 4 for face in output.data.polygons)
    assert original_vertices == tuple(tuple(vertex.co) for vertex in source.data.vertices)
    assert original_faces == tuple(tuple(face.vertices) for face in source.data.polygons)
    output.name = f"결과 — 쿼드 {len(output.data.polygons)}개"
    output.location.x = 2.0
    output.show_wire = True
    output.show_all_edges = True

    for obj, color in ((source, (0.3, 0.47, 0.62, 1.0)), (output, (0.3, 0.68, 0.53, 1.0))):
        obj.color = color
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            space.show_region_ui = True
            space.overlay.show_stats = True
            space.shading.color_type = "OBJECT"
            space.region_3d.view_distance = 12.0
            space.region_3d.view_location = (0.0, 0.0, 0.0)
            space.region_3d.view_rotation = Vector((0.0, -1.0, 0.35)).to_track_quat("Z", "Y")
            space.region_3d.view_perspective = "ORTHO"

    path = Path(__file__).resolve().parents[1] / "dist" / "3DRemesher_Prototype.blend"
    path.parent.mkdir(exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    print(f"데모 저장 완료: {path}")
    print(props.last_report)


if __name__ == "__main__":
    main()
