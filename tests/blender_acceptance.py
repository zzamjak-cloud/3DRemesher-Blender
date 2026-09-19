from __future__ import annotations

import importlib
import json
import math
import sys
from pathlib import Path

import bpy


ADDON_MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
TOLERANCE = 1.0e-4
RESULTS = []


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def _clear_scene():
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.ops.object.mode_set.poll() else None
    for obj in tuple(bpy.context.scene.objects):
        obj.select_set(True)
    bpy.ops.object.delete()


def _activate(obj):
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()


def _mesh_snapshot(obj):
    return (
        tuple(tuple(float(component) for component in vertex.co) for vertex in obj.data.vertices),
        tuple(tuple(poly.vertices) for poly in obj.data.polygons),
        obj.matrix_world.copy(),
    )


def _assert_source_unchanged(obj, snapshot):
    vertices, faces, matrix = snapshot
    assert_true(tuple(tuple(float(component) for component in vertex.co) for vertex in obj.data.vertices) == vertices, "원본 정점이 변경되었습니다.")
    assert_true(tuple(tuple(poly.vertices) for poly in obj.data.polygons) == faces, "원본 면이 변경되었습니다.")
    assert_true(obj.matrix_world == matrix, "원본 변환이 변경되었습니다.")


def _run_remesh(obj, *, target=64, symmetry=(), density=False, guide=False, expect_closed=False):
    _activate(obj)
    props = bpy.context.scene.zzamjak_3d_remesher
    props.target_quad_count = target
    props.symmetry_x = "X" in symmetry
    props.symmetry_y = "Y" in symmetry
    props.symmetry_z = "Z" in symmetry
    props.density_attribute_name = "remesh_density"
    props.density_scale = 6.0 if density else 1.0
    if density:
        _paint_density(obj)
    if guide:
        _add_guide(obj)

    before = _mesh_snapshot(obj)
    result = bpy.ops.object.zzamjak_3d_remesher_run()
    assert_true(result == {"FINISHED"}, f"리메시 실행 실패: {props.last_report}")
    result_obj = bpy.context.object
    assert_true(result_obj is not obj, "결과 오브젝트가 원본을 재사용했습니다.")
    assert_true(result_obj.data is not obj.data, "결과 메시 데이터가 원본을 재사용했습니다.")
    _assert_source_unchanged(obj, before)
    _assert_all_quads(result_obj)
    _assert_manifold_or_boundary(result_obj)
    if expect_closed:
        _assert_closed(result_obj)
    assert_true("미지원 제어" not in props.last_report, f"지원해야 할 제어가 미지원으로 보고되었습니다: {props.last_report}")
    RESULTS.append({"name":obj.name,"input_faces":len(obj.data.polygons),"output_quads":len(result_obj.data.polygons),"target":target,"symmetry":symmetry,"density":density,"guide":guide,"report":props.last_report})
    return result_obj, props.last_report


def _paint_density(obj):
    mesh = obj.data
    attribute = mesh.color_attributes.get("remesh_density")
    if attribute is None:
        attribute = mesh.color_attributes.new(name="remesh_density", type="FLOAT_COLOR", domain="POINT")
    for item_index, item in enumerate(attribute.data):
        vertex = mesh.vertices[item_index]
        item.color = (1.0, 1.0, 1.0, 1.0) if vertex.co.x >= 0 else (0.0, 0.0, 0.0, 1.0)


def _add_guide(obj):
    curve = bpy.data.curves.new(f"REMESH_GUIDE_{obj.name}", "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(2)
    spline.points[0].co = (-1.0, 0.0, 0.0, 1.0)
    spline.points[1].co = (0.0, 0.75, 0.0, 1.0)
    spline.points[2].co = (1.0, 0.0, 0.0, 1.0)
    guide_obj = bpy.data.objects.new(curve.name, curve)
    guide_obj.matrix_world = obj.matrix_world.copy()
    bpy.context.collection.objects.link(guide_obj)


def _assert_all_quads(obj):
    assert_true(obj.data.polygons, "결과 메시 면이 없습니다.")
    assert_true(all(len(poly.vertices) == 4 for poly in obj.data.polygons), "결과 메시가 쿼드가 아닌 면을 포함합니다.")


def _assert_manifold_or_boundary(obj):
    assert_true(all(count <= 2 for count in _edge_counts(obj).values()), "결과 메시가 비다양체 엣지를 포함합니다.")


def _assert_closed(obj):
    assert_true(all(count == 2 for count in _edge_counts(obj).values()), "결과 폐곡면에 경계 엣지가 있습니다.")


def _edge_counts(obj):
    edge_counts = {}
    for poly in obj.data.polygons:
        vertices = tuple(poly.vertices)
        for first, second in zip(vertices, (*vertices[1:], vertices[0])):
            edge = tuple(sorted((first, second)))
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    return edge_counts


def _assert_reduced(source_obj, result_obj):
    assert_true(len(result_obj.data.polygons) < len(source_obj.data.polygons), "결과 면 수가 원본보다 줄지 않았습니다.")


def _assert_symmetric(obj, axes):
    vertices = [vertex.co.copy() for vertex in obj.data.vertices]
    for axis_index, axis_name in enumerate(("X", "Y", "Z")):
        if axis_name not in axes:
            continue
        positive = max(vertex[axis_index] for vertex in vertices)
        negative = min(vertex[axis_index] for vertex in vertices)
        assert_true(abs(positive + negative) <= TOLERANCE, f"{axis_name} 대칭 bounds가 맞지 않습니다.")
        for vertex in vertices:
            mirror = vertex.copy()
            mirror[axis_index] *= -1.0
            assert_true(any((candidate - mirror).length <= TOLERANCE for candidate in vertices), f"{axis_name} 미러 정점을 찾지 못했습니다.")


def _cube():
    mesh = bpy.data.meshes.new("AcceptanceCubeMesh")
    mesh.from_pydata(
        [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
        [],
        [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)],
    )
    mesh.update()
    obj = bpy.data.objects.new("AcceptanceCube", mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def _subdivided_sphere():
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1.0, location=(0, 0, 0))
    obj = bpy.context.object
    obj.name = "AcceptanceSphere"
    return obj


def _torus():
    bpy.ops.mesh.primitive_torus_add(major_segments=48, minor_segments=16, major_radius=1.0, minor_radius=0.25)
    obj = bpy.context.object
    obj.name = "AcceptanceTorus"
    return obj


def _face_stats_by_x(obj):
    totals = {"positive": [0.0, 0], "negative": [0.0, 0]}
    for poly in obj.data.polygons:
        center_x = sum(obj.data.vertices[index].co.x for index in poly.vertices) / len(poly.vertices)
        area = _polygon_area(obj, poly.vertices)
        bucket = "positive" if center_x >= 0.0 else "negative"
        totals[bucket][0] += area
        totals[bucket][1] += 1
    return {
        key: {"mean_area": total / max(1, count), "count": count}
        for key, (total, count) in totals.items()
    }


def _polygon_area(obj, vertex_indices):
    vertices = [obj.data.vertices[index].co for index in vertex_indices]
    origin = vertices[0]
    area = 0.0
    for index in range(1, len(vertices) - 1):
        area += 0.5 * ((vertices[index] - origin).cross(vertices[index + 1] - origin)).length
    return area


def _assert_density_bias(uniform_area, density_obj):
    density_area = _face_stats_by_x(density_obj)
    uniform_area_ratio = uniform_area["positive"]["mean_area"] / max(TOLERANCE, uniform_area["negative"]["mean_area"])
    density_area_ratio = density_area["positive"]["mean_area"] / max(TOLERANCE, density_area["negative"]["mean_area"])
    uniform_count_ratio = uniform_area["positive"]["count"] / max(1, uniform_area["negative"]["count"])
    density_count_ratio = density_area["positive"]["count"] / max(1, density_area["negative"]["count"])
    area_improved = density_area_ratio < uniform_area_ratio * 0.95
    count_improved = density_count_ratio > uniform_count_ratio * 1.10
    assert_true(
        area_improved or count_improved,
        (
            "밀도 입력이 +X 영역 분포를 충분히 바꾸지 못했습니다. "
            f"area uniform={uniform_area_ratio:.3f}, density={density_area_ratio:.3f}; "
            f"count uniform={uniform_count_ratio:.3f}, density={density_count_ratio:.3f}"
        ),
    )


def main():
    importlib.import_module(ADDON_MODULE)
    operators = importlib.import_module(f"{ADDON_MODULE}.addon.operators")
    assert_true(ADDON_MODULE in bpy.context.preferences.addons, "애드온이 활성화되지 않았습니다.")

    _clear_scene()
    sphere = _subdivided_sphere()
    sphere_result, sphere_report = _run_remesh(sphere, target=128, expect_closed=True)
    _assert_reduced(sphere, sphere_result)
    assert_true("실제 쿼드 수" in sphere_report, "품질 보고에 실제 쿼드 수가 없습니다.")

    _clear_scene()
    symmetric_sphere = _subdivided_sphere()
    symmetric_result, _ = _run_remesh(symmetric_sphere, target=128, symmetry=("X", "Y", "Z"), expect_closed=True)
    _assert_symmetric(symmetric_result, ("X", "Y", "Z"))

    _clear_scene()
    cube = _cube()
    cube_result, _report = _run_remesh(cube, target=24, symmetry=("X", "Y", "Z"), expect_closed=True)
    _assert_symmetric(cube_result, ("X", "Y", "Z"))

    _clear_scene()
    uniform_torus = _torus()
    uniform_result, _report = _run_remesh(uniform_torus, target=128, guide=True, expect_closed=True)
    _assert_reduced(uniform_torus, uniform_result)
    uniform_area = _face_stats_by_x(uniform_result)

    _clear_scene()
    density_torus = _torus()
    _paint_density(density_torus)
    density_input, _warnings = operators._build_engine_input(bpy.context, density_torus)
    positive_values = [value for vertex, value in zip(density_torus.data.vertices, density_input.density_values) if vertex.co.x >= 0.0]
    negative_values = [value for vertex, value in zip(density_torus.data.vertices, density_input.density_values) if vertex.co.x < 0.0]
    assert_true(min(positive_values) > max(negative_values), "Blender RGB 밀도 추출이 +X와 -X를 구분하지 못했습니다.")
    density_result, _report = _run_remesh(density_torus, target=128, density=True, guide=True, expect_closed=True)
    _assert_reduced(density_torus, density_result)
    _assert_density_bias(uniform_area, density_result)

    path=Path(__file__).resolve().parents[1]/"dist"/"acceptance_results.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(RESULTS,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Blender acceptance test passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Blender acceptance test failed: {exc}", file=sys.stderr)
        raise
