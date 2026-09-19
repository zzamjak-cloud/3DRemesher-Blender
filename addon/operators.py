from __future__ import annotations

import bpy

from .blender_adapter import (
    collect_guide_curves,
    density_values_from_mesh,
    ensure_density_attribute,
    mesh_data_from_object,
    settings_from_scene,
)
from .core import RemeshBackend


def _active_mesh_object(context):
    obj = context.object
    if obj is None or obj.type != "MESH":
        return None
    return obj


def _require_object_mode(operator, context, action: str) -> bool:
    if context.mode == "OBJECT":
        return True
    operator.report({"ERROR"}, f"{action}은 오브젝트 모드에서 실행해야 합니다.")
    return False


class ZJREMESH_OT_analyze(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_analyze"
    bl_label = "메시 분석"
    bl_description = "선택한 메시를 리메시 엔진 입력으로 변환하고 품질 지표를 계산합니다"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "메시 분석"):
            return {"CANCELLED"}

        try:
            mesh_data = mesh_data_from_object(obj)
            settings = settings_from_scene(context.scene)
            guides = collect_guide_curves(context.scene, obj)
            density_values = density_values_from_mesh(obj.data, settings.density_attribute_name, settings.density_scale)
            engine_input = RemeshBackend().build_input(mesh_data, settings, guides, density_values)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        context.scene.zzamjak_3d_remesher.last_report = engine_input.analysis.summary_ko()
        self.report({"INFO"}, engine_input.analysis.summary_ko())
        return {"FINISHED"}


class ZJREMESH_OT_prepare_density(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_prepare_density"
    bl_label = "밀도 속성 준비"
    bl_description = "선택한 메시의 점 도메인 FLOAT_COLOR 밀도 속성을 준비합니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "밀도 속성 준비"):
            return {"CANCELLED"}
        if obj.data.users > 1:
            self.report({"ERROR"}, "공유 메시 데이터입니다. 단일 사용자 메시로 만든 뒤 실행하세요.")
            return {"CANCELLED"}

        props = context.scene.zzamjak_3d_remesher
        try:
            attribute = ensure_density_attribute(obj.data, props.density_attribute_name)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        message = f"밀도 속성 준비 완료: {attribute.name}"
        props.last_report = message
        self.report({"INFO"}, message)
        return {"FINISHED"}


class ZJREMESH_OT_run(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_run"
    bl_label = "리메시 실행"
    bl_description = "현재 입력을 검증합니다. 전문 리메시 엔진은 아직 연결되지 않았습니다"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = _active_mesh_object(context)
        if obj is None:
            self.report({"ERROR"}, "메시 오브젝트를 선택해야 합니다.")
            return {"CANCELLED"}
        if not _require_object_mode(self, context, "리메시 실행"):
            return {"CANCELLED"}

        try:
            settings = settings_from_scene(context.scene)
            engine_input = RemeshBackend().build_input(
                mesh_data_from_object(obj),
                settings,
                collect_guide_curves(context.scene, obj),
                density_values_from_mesh(obj.data, settings.density_attribute_name, settings.density_scale),
            )
            RemeshBackend().remesh(engine_input)
        except NotImplementedError as exc:
            context.scene.zzamjak_3d_remesher.last_report = str(exc)
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        return {"FINISHED"}


CLASSES = (
    ZJREMESH_OT_analyze,
    ZJREMESH_OT_prepare_density,
    ZJREMESH_OT_run,
)
