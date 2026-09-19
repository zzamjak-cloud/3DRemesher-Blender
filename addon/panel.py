from __future__ import annotations

import textwrap

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
        layout.label(text="위상·특징선에 따라 실제 개수 차이")

        row = layout.row(align=True)
        row.label(text="대칭")
        row.prop(props, "symmetry_x", toggle=True)
        row.prop(props, "symmetry_y", toggle=True)
        row.prop(props, "symmetry_z", toggle=True)
        layout.label(text="로컬 양의 축 기준 절단 후 미러")

        layout.prop(props, "hard_edge_angle")
        layout.prop(props, "density_attribute_name")
        layout.prop(props, "density_scale")
        layout.label(text="속성이 없으면 균일 밀도")

        layout.label(text="가이드 커브: REMESH_GUIDE_ 접두사")
        layout.label(text="엔진: 적응형 쿼드 리메시")

        layout.operator("object.zzamjak_3d_remesher_prepare_density", icon="GROUP_VCOL")
        layout.operator("object.zzamjak_3d_remesher_analyze", icon="VIEWZOOM")
        run_row = layout.row()
        run_row.enabled = not props.run_busy
        run_row.operator("object.zzamjak_3d_remesher_run", icon="MOD_REMESH")

        if props.run_busy:
            layout.prop(props, "run_progress", text="진행률", slider=True)
            layout.label(text=props.run_progress_message)
            layout.label(text="ESC로 취소")

        box = layout.box()
        box.label(text="최근 상태")
        for line in _wrapped_report_lines(props.last_report, context):
            box.label(text=line)


CLASSES = (ZJREMESH_PT_sidebar,)


def _wrapped_report_lines(report: str, context):
    width = getattr(getattr(context, "region", None), "width", 260)
    max_chars = max(22, min(54, int(width / 7)))
    lines = []
    for line in report.splitlines() or (report,):
        if not line:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(line, width=max_chars, break_long_words=False) or [line])
    return lines
