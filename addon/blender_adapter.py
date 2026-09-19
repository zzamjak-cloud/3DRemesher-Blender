from __future__ import annotations

from math import degrees, isfinite

from .core import GuideCurveData, MeshData, RemeshSettings


def mesh_data_from_object(obj) -> MeshData:
    mesh = obj.data
    vertices = tuple(tuple(vertex.co) for vertex in mesh.vertices)
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
    )


def collect_guide_curves(scene, target_obj=None) -> tuple[GuideCurveData, ...]:
    guides: list[GuideCurveData] = []
    target_inverse = target_obj.matrix_world.inverted() if target_obj is not None else None
    for obj in scene.objects:
        if obj.type != "CURVE" or not obj.name.startswith("REMESH_GUIDE_"):
            continue
        splines = []
        for spline in obj.data.splines:
            points = []
            source_points = spline.bezier_points if spline.bezier_points else spline.points
            for point in source_points:
                co = point.co
                if len(co) == 4:
                    world_point = obj.matrix_world @ co.to_3d()
                else:
                    world_point = obj.matrix_world @ co
                local_point = target_inverse @ world_point if target_inverse is not None else world_point
                points.append(tuple(local_point))
            if points:
                splines.append(tuple(points))
        guides.append(GuideCurveData(name=obj.name, splines=tuple(splines)))
    return tuple(guides)


def density_values_from_mesh(mesh, attribute_name: str, scale: float = 1.0) -> tuple[float, ...]:
    if not attribute_name.strip():
        raise ValueError("밀도 속성 이름은 비어 있을 수 없습니다.")
    attribute = mesh.color_attributes.get(attribute_name)
    generic_attribute = mesh.attributes.get(attribute_name)
    if attribute is None:
        if generic_attribute is not None:
            raise ValueError(f"'{attribute_name}' 속성이 밀도 컬러 속성이 아닙니다.")
        return ()
    if attribute.domain != "POINT" or getattr(attribute, "data_type", "FLOAT_COLOR") != "FLOAT_COLOR":
        raise ValueError(f"'{attribute_name}' 속성은 점 도메인 FLOAT_COLOR여야 합니다.")
    values = []
    for index, color in enumerate(attribute.data):
        value = float(color.color[0]) * scale
        if not isfinite(value):
            raise ValueError(f"{index}번 밀도 값은 유한한 숫자여야 합니다.")
        values.append(value)
    return tuple(values)


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
