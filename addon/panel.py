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

        layout.prop(props, "topology_mode")
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
        layout.label(text="튜브: LOOP=둘레, STRIP_=세로")
        _draw_ring_guides(layout, context)
        if props.topology_mode == "LEGACY":
            layout.label(text="실험 엔진: 루프 흐름 미보장", icon="INFO")
        elif props.topology_mode == "STRUCTURED":
            layout.label(text="격자 미지원 형상은 실행 중단", icon="INFO")
        elif props.topology_mode == "QUADRIFLOW":
            layout.label(text="복셀 리메시 후 QuadriFlow, LOOP=절단 링", icon="INFO")

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


RING_GUIDE_LIST_LIMIT = 8  # 패널은 매 리드로우마다 그려지므로 목록을 이 개수까지만 보여 준다


def _draw_ring_guides(layout, context) -> None:
    from .ring_geometry import RIM_OK_RATIO
    from .ring_guide import ring_guides

    box = layout.box()
    box.label(text="링 가이드 (팔·다리·목)")
    row = box.row(align=True)
    row.operator("object.zzamjak_3d_remesher_add_ring_guide", icon="CURVE_NCIRCLE")
    row.operator("object.zzamjak_3d_remesher_check_ring_guides", text="", icon="FILE_REFRESH")
    row.operator("object.zzamjak_3d_remesher_clear_ring_guides", text="", icon="TRASH")
    box.label(text="좌클릭 추가 · Ctrl+Z 마지막 취소 · 우클릭 종료")
    active = context.active_object
    active_props = getattr(active, "zzamjak_ring_guide", None) if active is not None and active.type == "CURVE" else None
    if active_props is not None and active_props.is_ring:
        box.prop(active_props, "offset")
        row = box.row(align=True)
        row.label(text=f"{active_props.status} (둘레 비율 {active_props.ratio:.2f})", icon="CHECKMARK" if active_props.ratio >= RIM_OK_RATIO else "ERROR")
        row.operator("object.zzamjak_3d_remesher_remove_ring_guide", text="", icon="X").name = active.name
    others = [guide for guide in ring_guides(context.scene) if guide is not active]
    for guide in others[:RING_GUIDE_LIST_LIMIT]:
        props = guide.zzamjak_ring_guide
        row = box.row(align=True)
        row.label(text=f"{guide.name}  {props.ratio:.2f}", icon="CHECKMARK" if props.ratio >= RIM_OK_RATIO else "ERROR")
        row.operator("object.zzamjak_3d_remesher_remove_ring_guide", text="", icon="X").name = guide.name
    if len(others) > RING_GUIDE_LIST_LIMIT:
        box.label(text=f"외 {len(others) - RING_GUIDE_LIST_LIMIT}개")


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
