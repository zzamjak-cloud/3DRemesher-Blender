from __future__ import annotations

import bpy


class ZJREMESH_PT_sidebar(bpy.types.Panel):
    bl_label = "3D Remesher"
    bl_idname = "ZJREMESH_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "3D Remesher"

    def draw(self, context):
        layout = self.layout
        props = context.scene.zzamjak_3d_remesher

        layout.prop(props, "target_quad_count")

        row = layout.row(align=True)
        row.label(text="대칭")
        row.prop(props, "symmetry_x", toggle=True)
        row.prop(props, "symmetry_y", toggle=True)
        row.prop(props, "symmetry_z", toggle=True)

        layout.prop(props, "hard_edge_angle")
        layout.prop(props, "density_attribute_name")
        layout.prop(props, "density_scale")

        layout.label(text="가이드 커브: REMESH_GUIDE_ 접두사")
        layout.label(text="엔진: 입력 검증만 가능")

        layout.operator("object.zzamjak_3d_remesher_prepare_density", icon="GROUP_VCOL")
        layout.operator("object.zzamjak_3d_remesher_analyze", icon="VIEWZOOM")
        layout.operator("object.zzamjak_3d_remesher_run", icon="MOD_REMESH")

        box = layout.box()
        box.label(text="최근 상태")
        box.label(text=props.last_report)


CLASSES = (ZJREMESH_PT_sidebar,)
