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
        layout.label(text="목표 수는 가장 가까운 분할 수로 처리됩니다.")

        row = layout.row(align=True)
        row.label(text="대칭")
        row.enabled = False
        row.prop(props, "symmetry_x", toggle=True)
        row.prop(props, "symmetry_y", toggle=True)
        row.prop(props, "symmetry_z", toggle=True)
        layout.label(text="대칭: 준비 중")

        layout.prop(props, "hard_edge_angle")
        density_box = layout.box()
        density_box.enabled = False
        density_box.prop(props, "density_attribute_name")
        density_box.prop(props, "density_scale")
        layout.label(text="밀도 입력: 준비 중")

        layout.label(text="가이드 커브: REMESH_GUIDE_ 접두사")
        layout.label(text="엔진: 실험용 쿼드 생성")

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
