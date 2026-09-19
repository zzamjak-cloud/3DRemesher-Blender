from __future__ import annotations

from math import degrees, isfinite

import bpy
from mathutils.geometry import interpolate_bezier

from .core import (
    GUIDE_KIND_DIRECTION,
    GUIDE_KIND_LOOP,
    GUIDE_KIND_VALUES,
    GuideCurveData,
    MeshData,
    RemeshSettings,
)


def mesh_data_from_object(obj) -> MeshData:
    mesh = obj.data
    vertices = tuple(tuple(float(component) for component in vertex.co) for vertex in mesh.vertices)
    faces = tuple(tuple(polygon.vertices) for polygon in mesh.polygons)
    hard_edges = frozenset(
        tuple(sorted(edge.vertices))
        for edge in mesh.edges
        if edge.use_seam or getattr(edge, "use_edge_sharp", False)
    )
    return MeshData(vertices=vertices, faces=faces, hard_edges=hard_edges)


def settings_from_scene(scene) -> RemeshSettings:
    props = scene.zzamjak_3d_remesher
    axes = tuple(
        axis
        for axis, enabled in (
            ("X", props.symmetry_x),
            ("Y", props.symmetry_y),
            ("Z", props.symmetry_z),
        )
        if enabled
    )
    return RemeshSettings(
        target_quad_count=props.target_quad_count,
        symmetry_axes=axes,
        hard_edge_angle_degrees=degrees(props.hard_edge_angle),
        guide_curve_names=tuple(_guide_curve_names(scene)),
        density_attribute_name=props.density_attribute_name,
        density_scale=props.density_scale,
        topology_mode=getattr(props, "topology_mode", "AUTO"),
    )


def unsupported_control_warnings(scene) -> tuple[str, ...]:
    return ()


def collect_guide_curves(scene, target_obj=None) -> tuple[GuideCurveData, ...]:
    guides: list[GuideCurveData] = []
    try:
        target_inverse = target_obj.matrix_world.inverted() if target_obj is not None else None
    except ValueError as exc:
        raise ValueError("선택한 메시의 월드 변환을 역변환할 수 없습니다.") from exc
    for obj in scene.objects:
        if obj.type != "CURVE" or not obj.name.startswith("REMESH_GUIDE_"):
            continue
        splines = []
        kind = []
        closed = []
        object_kind = _guide_kind_from_object(obj)
        for spline in obj.data.splines:
            is_closed = bool(getattr(spline, "use_cyclic_u", False))
            points = _sample_curve_spline(obj, spline, target_inverse)
            if points:
                splines.append(tuple(points))
                kind.append(object_kind or (GUIDE_KIND_LOOP if is_closed else GUIDE_KIND_DIRECTION))
                closed.append(is_closed)
        guides.append(GuideCurveData(name=obj.name, splines=tuple(splines), kind=tuple(kind), closed=tuple(closed)))
    return tuple(guides)


def build_result_object(source_obj, mesh_data: MeshData, *, name_suffix: str = "_remesh"):
    mesh_data.validate()
    mesh = bpy.data.meshes.new(f"{source_obj.data.name}{name_suffix}")
    obj = None
    try:
        mesh.from_pydata(mesh_data.vertices, [], mesh_data.faces)
        mesh.update(calc_edges=True)
        if mesh.validate(clean_customdata=False):
            raise ValueError("엔진 결과 메시가 Blender 검증을 통과하지 못했습니다.")
        _apply_hard_edges(mesh, mesh_data.hard_edges)
        for material in source_obj.data.materials:
            mesh.materials.append(material)

        obj = bpy.data.objects.new(f"{source_obj.name}{name_suffix}", mesh)
        obj.matrix_world = source_obj.matrix_world.copy()
        obj.hide_viewport = False
        obj.hide_render = False
        obj.display_type = "TEXTURED"
        return obj
    except Exception:
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        raise


def link_result_object(context, source_obj, result_obj) -> None:
    collections = source_obj.users_collection
    collection = collections[0] if collections else context.collection
    collection.objects.link(result_obj)
    for selected in context.selected_objects:
        selected.select_set(False)
    result_obj.select_set(True)
    context.view_layer.objects.active = result_obj
    context.view_layer.update()


def result_mesh_data(remesh_result) -> MeshData:
    mesh_data = getattr(remesh_result, "mesh", remesh_result)
    if not isinstance(mesh_data, MeshData):
        raise ValueError("엔진 결과에 메시 데이터가 없습니다.")
    return mesh_data


def format_result_report(remesh_result, extra_warnings: tuple[str, ...] = ()) -> str:
    quality = getattr(remesh_result, "quality", None)
    warnings = [str(item) for item in extra_warnings if str(item)]
    warnings.extend(str(item) for item in getattr(remesh_result, "warnings", ()) if str(item))
    warnings.extend(
        f"미지원 제어: {_control_label(str(item))}"
        for item in getattr(remesh_result, "unsupported_controls", ())
        if str(item)
    )

    lines = ["리메시 완료"]
    if quality is not None:
        lines.extend(_format_quality_lines(quality))
    elif isinstance(remesh_result, MeshData):
        actual_quad_count = sum(1 for face in remesh_result.faces if len(face) == 4)
        lines.append(f"실제 쿼드 수: {actual_quad_count}")
    if warnings:
        lines.append("알림:")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def format_input_report(engine_input, extra_warnings: tuple[str, ...] = ()) -> str:
    lines = [
        engine_input.analysis.summary_ko(),
        f"목표 쿼드 수: {engine_input.settings.target_quad_count} (위상·특징선에 따라 실제 개수 차이)",
    ]
    if extra_warnings:
        lines.append("알림:")
        lines.extend(f"- {warning}" for warning in extra_warnings)
    return "\n".join(lines)


def _sample_curve_spline(obj, spline, target_inverse):
    if spline.type == "POLY":
        points = [_local_curve_point(obj, point.co.to_3d(), target_inverse) for point in spline.points]
        if spline.use_cyclic_u and points:
            points.append(points[0])
        return tuple(points)
    if spline.type == "BEZIER":
        return tuple(_sample_bezier_points(obj, spline, target_inverse))
    raise ValueError(f"{obj.name}의 {spline.type} 가이드 스플라인은 아직 지원하지 않습니다.")


def _guide_kind_from_object(obj) -> str | None:
    for owner in (obj, obj.data):
        for key in ("remesh_guide_kind", "guide_kind", "zzamjak_guide_kind"):
            if key in owner:
                return _normalize_guide_kind(owner[key], f"{obj.name} {key}")
    suffix = obj.name.removeprefix("REMESH_GUIDE_")
    token = suffix.split("_", 1)[0]
    if token.upper() in GUIDE_KIND_VALUES:
        return token.upper()
    return None


def _normalize_guide_kind(value, label: str) -> str:
    guide_kind = str(value).strip().upper()
    if guide_kind not in GUIDE_KIND_VALUES:
        raise ValueError(f"{label} 값은 LOOP, STRIP, DIRECTION 중 하나여야 합니다.")
    return guide_kind


def _sample_bezier_points(obj, spline, target_inverse):
    bezier_points = spline.bezier_points
    if not bezier_points:
        return ()
    if len(bezier_points) == 1:
        return (_local_curve_point(obj, bezier_points[0].co, target_inverse),)

    segment_count = len(bezier_points) if spline.use_cyclic_u else len(bezier_points) - 1
    resolution = max(1, int(getattr(spline, "resolution_u", 12) or obj.data.resolution_u or 12))
    sampled = []
    for index in range(segment_count):
        first = bezier_points[index]
        second = bezier_points[(index + 1) % len(bezier_points)]
        segment = interpolate_bezier(first.co, first.handle_right, second.handle_left, second.co, resolution + 1)
        if sampled:
            segment = segment[1:]
        sampled.extend(_local_curve_point(obj, point, target_inverse) for point in segment)
    return tuple(sampled)


def _local_curve_point(obj, point, target_inverse):
    world_point = obj.matrix_world @ point
    local_point = target_inverse @ world_point if target_inverse is not None else world_point
    return tuple(float(component) for component in local_point)


def _apply_hard_edges(mesh, hard_edges):
    if not hard_edges:
        return
    hard_edge_lookup = {tuple(sorted(edge)) for edge in hard_edges}
    for edge in mesh.edges:
        if tuple(sorted(edge.vertices)) in hard_edge_lookup:
            edge.use_seam = True
            edge.use_edge_sharp = True


def _format_quality_lines(quality) -> list[str]:
    field_labels = (
        ("target_quad_count", "목표 쿼드 수"),
        ("actual_quad_count", "실제 쿼드 수"),
        ("target_error_ratio", "목표 오차율"),
        ("quad_ratio", "쿼드 비율"),
        ("boundary_edge_count", "경계 엣지"),
        ("non_manifold_edge_count", "비다양체 엣지"),
        ("degenerate_face_count", "퇴화 면"),
        ("max_aspect_ratio", "최대 종횡비"),
        ("mean_aspect_ratio", "평균 종횡비"),
        ("max_surface_error", "최대 표면 오차"),
        ("mean_surface_error", "평균 표면 오차"),
        ("symmetry_error", "대칭 오차"),
        ("field_alignment", "필드 정렬도"),
    )
    lines = []
    for field_name, label in field_labels:
        if not hasattr(quality, field_name):
            continue
        value = getattr(quality, field_name)
        if field_name in {"quad_ratio", "target_error_ratio", "field_alignment"}:
            lines.append(f"{label}: {value:.1%}")
        elif field_name in {"max_surface_error", "mean_surface_error", "symmetry_error"}:
            lines.append(f"{label}: {value:.6g}")
        elif isinstance(value, float):
            lines.append(f"{label}: {value:.3f}")
        else:
            lines.append(f"{label}: {value}")
    return lines


def _control_label(control: str) -> str:
    labels = {
        "density": "밀도 입력",
        "density_attribute": "밀도 입력",
        "density_scale": "밀도 대비",
        "symmetry": "대칭",
        "symmetry_axes": "대칭 축",
        "target_quad_count": "정확한 목표 쿼드 수",
        "adaptive_density": "적응 밀도",
    }
    return labels.get(control, control)


def density_values_from_mesh(mesh, attribute_name: str, scale: float = 1.0) -> tuple[float, ...]:
    if not attribute_name.strip():
        raise ValueError("밀도 속성 이름은 비어 있을 수 없습니다.")
    attribute = mesh.color_attributes.get(attribute_name)
    generic_attribute = mesh.attributes.get(attribute_name)
    if attribute is None:
        if generic_attribute is not None:
            raise ValueError(f"'{attribute_name}' 속성이 밀도 컬러 속성이 아닙니다.")
        return tuple(1.0 * scale for _vertex in mesh.vertices)
    if attribute.domain != "POINT" or getattr(attribute, "data_type", "FLOAT_COLOR") != "FLOAT_COLOR":
        raise ValueError(f"'{attribute_name}' 속성은 점 도메인 FLOAT_COLOR여야 합니다.")
    values = []
    for index, color in enumerate(attribute.data):
        red, green, blue = (float(color.color[0]), float(color.color[1]), float(color.color[2]))
        value = (0.2126 * red + 0.7152 * green + 0.0722 * blue) * scale
        if not isfinite(value):
            raise ValueError(f"{index}번 밀도 값은 유한한 숫자여야 합니다.")
        values.append(value)
    return tuple(values)


extract_density_values = density_values_from_mesh


def ensure_density_attribute(mesh, attribute_name: str):
    if not attribute_name.strip():
        raise ValueError("밀도 속성 이름은 비어 있을 수 없습니다.")
    attribute = mesh.color_attributes.get(attribute_name)
    generic_attribute = mesh.attributes.get(attribute_name)
    if attribute is None and generic_attribute is not None:
        raise ValueError(f"'{attribute_name}' 이름의 다른 속성이 이미 있습니다.")
    if attribute is not None:
        if attribute.domain != "POINT" or getattr(attribute, "data_type", "FLOAT_COLOR") != "FLOAT_COLOR":
            raise ValueError(f"'{attribute_name}' 속성은 점 도메인 FLOAT_COLOR여야 합니다.")
        return attribute
    if attribute is None:
        attribute = mesh.color_attributes.new(name=attribute_name, type="FLOAT_COLOR", domain="POINT")
        for item in attribute.data:
            item.color = (1.0, 1.0, 1.0, 1.0)
    return attribute


def _guide_curve_names(scene):
    for obj in scene.objects:
        if obj.type == "CURVE" and obj.name.startswith("REMESH_GUIDE_"):
            yield obj.name
