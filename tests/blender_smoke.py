from __future__ import annotations

import importlib
import ast
import os
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import bpy


ADDON_MODULE = "bl_ext.user_default.zzamjak_3d_remesher"


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def assert_cancelled(operation, expected_message: str):
    try:
        result = operation()
    except RuntimeError as exc:
        assert_true(expected_message in str(exc), f"예상 오류 메시지가 아닙니다: {exc}")
        return
    assert_true(result == {"CANCELLED"}, "연산이 중단되지 않았습니다.")


def read_bl_info_version(init_path: Path) -> str:
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "bl_info" for target in node.targets):
            bl_info = ast.literal_eval(node.value)
            return ".".join(str(part) for part in bl_info["version"])
    raise AssertionError("bl_info를 찾을 수 없습니다.")


def main():
    module = importlib.import_module(ADDON_MODULE)
    adapter = importlib.import_module(f"{ADDON_MODULE}.addon.blender_adapter")
    operators = importlib.import_module(f"{ADDON_MODULE}.addon.operators")
    core = importlib.import_module(f"{ADDON_MODULE}.addon.core")
    assert_true(ADDON_MODULE in bpy.context.preferences.addons, "애드온이 활성화되지 않았습니다.")

    repo_root = Path(os.environ["REMESHER_DEV_ROOT"]).resolve()
    profile_root = Path(os.environ["REMESHER_DEV_PROFILE"]).resolve()
    extension_id = os.environ["REMESHER_EXTENSION_ID"]
    user_resource = Path(bpy.utils.resource_path("USER")).resolve()
    source_link = profile_root / "extensions" / "user_default" / extension_id
    manifest = tomllib.loads((repo_root / "blender_manifest.toml").read_text(encoding="utf-8"))
    bl_info_version = read_bl_info_version(repo_root / "__init__.py")

    assert_true(user_resource == profile_root, "Blender USER 프로필이 개발 프로필과 다릅니다.")
    assert_true(source_link.resolve() == repo_root, "개발 Extension 심링크가 저장소를 가리키지 않습니다.")
    assert_true(manifest["version"] == bl_info_version, "manifest와 bl_info 버전이 다릅니다.")

    mesh = bpy.data.meshes.new("SmokeMesh")
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    obj = bpy.data.objects.new("SmokeObject", mesh)
    obj.location = (10, 0, 0)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.context.view_layer.update()

    props = bpy.context.scene.zzamjak_3d_remesher
    props.target_quad_count = 12
    props.symmetry_x = True
    props.symmetry_y = False
    props.symmetry_z = False

    result = bpy.ops.object.zzamjak_3d_remesher_prepare_density()
    assert_true(result == {"FINISHED"}, "밀도 속성 준비 실패")
    density_attribute = mesh.color_attributes.get(props.density_attribute_name)
    assert_true(density_attribute is not None, "밀도 속성이 없습니다.")
    density_attribute.data[0].color = (0.25, 0.25, 0.25, 1.0)

    result = bpy.ops.object.zzamjak_3d_remesher_prepare_density()
    assert_true(result == {"FINISHED"}, "기존 밀도 속성 재사용 실패")
    assert_true(abs(density_attribute.data[0].color[0] - 0.25) < 1.0e-6, "기존 밀도 값을 덮어썼습니다.")

    props.density_attribute_name = " "
    assert_cancelled(bpy.ops.object.zzamjak_3d_remesher_prepare_density, "밀도 속성 이름")
    props.density_attribute_name = "remesh_density"

    conflict_mesh = bpy.data.meshes.new("ConflictMesh")
    conflict_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    conflict_mesh.update()
    conflict_mesh.attributes.new(name="density_conflict", type="FLOAT", domain="POINT")
    conflict_obj = bpy.data.objects.new("ConflictObject", conflict_mesh)
    bpy.context.collection.objects.link(conflict_obj)
    bpy.context.view_layer.objects.active = conflict_obj
    obj.select_set(False)
    conflict_obj.select_set(True)
    props.density_attribute_name = "density_conflict"
    assert_cancelled(bpy.ops.object.zzamjak_3d_remesher_prepare_density, "다른 속성")
    assert_true(conflict_mesh.color_attributes.get("density_conflict") is None, "충돌 속성 위에 컬러 속성을 만들면 안 됩니다.")

    conflict_obj.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    props.density_attribute_name = "remesh_density"

    guide_curve = bpy.data.curves.new("REMESH_GUIDE_smoke", "CURVE")
    guide_curve.dimensions = "3D"
    guide_spline = guide_curve.splines.new("POLY")
    guide_spline.points.add(1)
    guide_spline.points[0].co = (11, 0, 0, 1)
    guide_spline.points[1].co = (12, 0, 0, 1)
    guide_obj = bpy.data.objects.new("REMESH_GUIDE_smoke", guide_curve)
    guide_obj["remesh_guide_kind"] = "STRIP"
    bpy.context.collection.objects.link(guide_obj)
    loop_curve = bpy.data.curves.new("REMESH_GUIDE_LOOP_smoke", "CURVE")
    loop_curve.dimensions = "3D"
    loop_spline = loop_curve.splines.new("POLY")
    loop_spline.points.add(2)
    loop_spline.points[0].co = (11, 0, 0, 1)
    loop_spline.points[1].co = (12, 0, 0, 1)
    loop_spline.points[2].co = (11, 1, 0, 1)
    loop_spline.use_cyclic_u = True
    loop_obj = bpy.data.objects.new("REMESH_GUIDE_LOOP_smoke", loop_curve)
    bpy.context.collection.objects.link(loop_obj)
    guides = adapter.collect_guide_curves(bpy.context.scene, obj)
    guide_by_name = {guide.name: guide for guide in guides}
    assert_true(guide_by_name["REMESH_GUIDE_smoke"].splines[0] == ((1.0, 0.0, 0.0), (2.0, 0.0, 0.0)), "가이드 좌표가 선택 메시 로컬 좌표로 변환되지 않았습니다.")
    assert_true(guide_by_name["REMESH_GUIDE_smoke"].kind == ("STRIP",), "열린 STRIP 가이드 종류가 수집되지 않았습니다.")
    assert_true(guide_by_name["REMESH_GUIDE_smoke"].closed == (False,), "열린 STRIP 가이드 닫힘 상태가 잘못 수집되었습니다.")
    assert_true(guide_by_name["REMESH_GUIDE_LOOP_smoke"].kind == ("LOOP",), "닫힌 LOOP 가이드 종류가 수집되지 않았습니다.")
    assert_true(guide_by_name["REMESH_GUIDE_LOOP_smoke"].closed == (True,), "닫힌 LOOP 가이드 닫힘 상태가 수집되지 않았습니다.")

    bpy.ops.object.mode_set(mode="EDIT")
    assert_cancelled(bpy.ops.object.zzamjak_3d_remesher_analyze, "오브젝트 모드")
    bpy.ops.object.mode_set(mode="OBJECT")

    result = bpy.ops.object.zzamjak_3d_remesher_analyze()
    assert_true(result == {"FINISHED"}, "메시 분석 실패")
    assert_true("쿼드 1" in props.last_report, "분석 결과가 예상과 다릅니다.")
    engine_input, _warnings = operators._build_engine_input(bpy.context, obj)
    assert_true(engine_input.settings.symmetry_axes == ("X",), "대칭 축이 엔진 입력에 전달되지 않았습니다.")
    assert_true(len(engine_input.density_values) == len(mesh.vertices), "밀도 값이 엔진 입력에 전달되지 않았습니다.")
    assert_true(abs(engine_input.density_values[0] - 0.25) < 1.0e-6, "밀도 컬러 속성 값이 엔진 입력과 다릅니다.")
    payload = operators._engine_input_payload(engine_input)
    payload_guides = {guide["name"]: guide for guide in payload["guide_curves"]}
    assert_true(payload["settings"]["topology_mode"] == "AUTO", "기본 위상 모드가 worker payload에 들어가지 않았습니다.")
    assert_true(payload_guides["REMESH_GUIDE_smoke"]["kind"] == ["STRIP"], "가이드 종류가 worker payload에 들어가지 않았습니다.")
    assert_true(payload_guides["REMESH_GUIDE_LOOP_smoke"]["closed"] == [True], "가이드 닫힘 상태가 worker payload에 들어가지 않았습니다.")

    original_vertices = tuple(tuple(vertex.co) for vertex in mesh.vertices)
    original_faces = tuple(tuple(polygon.vertices) for polygon in mesh.polygons)
    original_matrix = obj.matrix_world.copy()
    original_material = bpy.data.materials.new("SmokeMaterial")
    obj.data.materials.append(original_material)

    class FakeBackend:
        def build_input(self, mesh_data, settings, guide_curves=(), density_values=()):
            return core.build_engine_input(mesh_data, settings, guide_curves, density_values)

        def remesh(self, engine_input, *, progress=None, cancelled=None):
            if progress is not None:
                progress(0.5, "테스트 리메시")
            if cancelled is not None and cancelled():
                raise RuntimeError("취소됨")
            result_mesh = core.MeshData(
                vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
                faces=((0, 1, 2, 3),),
                hard_edges=frozenset({(0, 1)}),
            )
            quality = SimpleNamespace(
                target_quad_count=engine_input.settings.target_quad_count,
                actual_quad_count=1,
                target_error_ratio=abs(engine_input.settings.target_quad_count - 1) / engine_input.settings.target_quad_count,
                quad_ratio=1.0,
                boundary_edge_count=4,
                non_manifold_edge_count=0,
                degenerate_face_count=0,
                max_aspect_ratio=1.0,
                mean_aspect_ratio=1.0,
                max_surface_error=0.0,
                mean_surface_error=0.0,
                symmetry_error=0.0,
                field_alignment=1.0,
            )
            return SimpleNamespace(
                mesh=result_mesh,
                quality=quality,
                warnings=("테스트 경고",),
                unsupported_controls=(),
            )

    original_backend = operators.RemeshBackend
    operators.RemeshBackend = FakeBackend
    result = bpy.ops.object.zzamjak_3d_remesher_run()
    operators.RemeshBackend = original_backend
    assert_true(result == {"FINISHED"}, "리메시 실행 실패")
    assert_true(tuple(tuple(vertex.co) for vertex in mesh.vertices) == original_vertices, "미구현 실행이 원본 정점을 바꾸면 안 됩니다.")
    assert_true(tuple(tuple(polygon.vertices) for polygon in mesh.polygons) == original_faces, "미구현 실행이 원본 면을 바꾸면 안 됩니다.")
    assert_true(obj.matrix_world == original_matrix, "리메시 실행이 원본 변환을 바꾸면 안 됩니다.")
    assert_true(obj.data.materials[0] == original_material, "리메시 실행이 원본 머티리얼을 바꾸면 안 됩니다.")
    result_obj = bpy.context.object
    assert_true(result_obj is not obj, "결과가 원본 오브젝트를 재사용하면 안 됩니다.")
    assert_true(result_obj.type == "MESH", "결과 오브젝트가 메시가 아닙니다.")
    assert_true(result_obj.data is not obj.data, "결과가 원본 메시 데이터를 재사용하면 안 됩니다.")
    assert_true(result_obj.matrix_world == original_matrix, "결과 오브젝트 변환이 원본과 다릅니다.")
    assert_true(result_obj.data.materials[0] == original_material, "결과 오브젝트에 원본 머티리얼이 복사되지 않았습니다.")
    assert_true("목표 쿼드 수: 12" in props.last_report, "목표 쿼드 수가 보고되지 않았습니다.")
    assert_true("실제 쿼드 수: 1" in props.last_report, "실제 쿼드 수가 보고되지 않았습니다.")

    obj.select_set(True)
    result_obj.select_set(False)
    bpy.context.view_layer.objects.active = obj
    object_names_before_failure = set(bpy.data.objects.keys())
    mesh_names_before_failure = set(bpy.data.meshes.keys())
    selection_before_failure = tuple(bpy.context.selected_objects)
    active_before_failure = bpy.context.view_layer.objects.active

    class FailingBackend(FakeBackend):
        def remesh(self, engine_input, *, progress=None, cancelled=None):
            raise ValueError("의도된 실패")

    operators.RemeshBackend = FailingBackend
    try:
        assert_cancelled(bpy.ops.object.zzamjak_3d_remesher_run, "의도된 실패")
    finally:
        operators.RemeshBackend = original_backend
    assert_true(set(bpy.data.objects.keys()) == object_names_before_failure, "실패한 리메시가 오브젝트를 남겼습니다.")
    assert_true(set(bpy.data.meshes.keys()) == mesh_names_before_failure, "실패한 리메시가 메시 데이터블록을 남겼습니다.")
    assert_true(tuple(bpy.context.selected_objects) == selection_before_failure, "실패한 리메시가 선택 상태를 바꿨습니다.")
    assert_true(bpy.context.view_layer.objects.active == active_before_failure, "실패한 리메시가 활성 오브젝트를 바꿨습니다.")

    dummy_job = operators._RunJob(
        source_name=obj.name,
        source_pointer=obj.as_pointer(),
        source_mesh_pointer=obj.data.as_pointer(),
        source_geometry_fingerprint=operators._source_input_fingerprint(obj, bpy.context.scene),
        scene_pointer=bpy.context.scene.as_pointer(),
        warnings=(),
        temp_dir=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job",
        input_path=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job" / "input.json",
        result_path=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job" / "result.json",
        progress_path=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job" / "progress.json",
        cancel_path=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job" / "cancel",
        stderr_path=Path(os.environ["REMESHER_DEV_PROFILE"]) / "dummy_cancel_job" / "worker.log",
    )
    operators._ACTIVE_JOB = dummy_job
    module.unregister()
    assert_true(operators._ACTIVE_JOB is None, "unregister 후 실행 작업 상태가 남아 있습니다.")
    assert_true(not hasattr(bpy.types.Scene, "zzamjak_3d_remesher"), "unregister 후 Scene 속성이 남아 있습니다.")
    module.register()
    assert_true(hasattr(bpy.types.Scene, "zzamjak_3d_remesher"), "register 후 Scene 속성이 없습니다.")
    # 같은 Blender 세션에서 이어 도는 다른 검사가 이 가이드를 필수 LOOP/STRIP 으로 읽지 않도록 지운다.
    for guide in (guide_obj, loop_obj):
        curve = guide.data
        bpy.data.objects.remove(guide, do_unlink=True)
        bpy.data.curves.remove(curve)
    assert_true(not adapter.collect_guide_curves(bpy.context.scene, obj), "스모크 가이드 커브가 정리되지 않았습니다.")
    print("Blender smoke test passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Blender smoke test failed: {exc}", file=sys.stderr)
        raise
