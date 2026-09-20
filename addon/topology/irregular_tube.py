from __future__ import annotations

from bisect import bisect_right
from math import atan2, cos, isclose, pi, sin, sqrt

from ..core import CancelledCallback, EngineInput, MeshData, Vector3
from .quality import LayoutExpectations, validate_layout
from .triangulated_limb import (
    EPSILON, _add, _boundary_loops, _check_cancelled, _closed_distance,
    _connected_and_manifold, _cross, _distance, _dot, _edge_faces, _guides,
    _mean, _mix, _open_distance, _scale, _sub, _unit,
)


MAX_SOURCE_FACES = 50000
MAX_OUTPUT_QUADS = 20000


def try_remesh_irregular_tube(
    engine_input: EngineInput, *, cancelled: CancelledCallback | None = None
) -> MeshData | None:
    """임의 삼각 분할의 단일 열린 관을 단면 교차로 다시 격자화한다.

    두 경계의 단면 축이 명확하고 모든 중간 단면이 하나의 별모양 폐곡선일 때만
    새 링을 만든다. 분기, 내부 빈 공간, 겹친 단면은 추측하지 않는다.
    """
    _check_cancelled(cancelled)
    mesh, settings = engine_input.mesh, engine_input.settings
    if (not mesh.faces or engine_input.analysis.degenerate_face_count or
            len(mesh.faces) > MAX_SOURCE_FACES or
            settings.target_quad_count > MAX_OUTPUT_QUADS or settings.symmetry_axes or
            mesh.hard_edges or not isclose(settings.density_scale, 1.0, abs_tol=EPSILON) or
            (settings.guide_curve_names and not engine_input.guide_curves) or
            (engine_input.density_values and
             max(engine_input.density_values) - min(engine_input.density_values) > EPSILON)):
        return None
    if any(len(face) != 3 or len(set(face)) != 3 for face in mesh.faces):
        return None
    edges = _edge_faces(mesh.faces)
    if any(len(owners) > 2 for owners in edges.values()):
        return None
    boundaries = _boundary_loops(tuple(edge for edge, owners in edges.items() if len(owners) == 1))
    if len(boundaries) != 2 or len(mesh.vertices) - len(edges) + len(mesh.faces) != 0:
        return None
    if not _connected_and_manifold(mesh, edges, boundaries):
        return None
    if not _consistent_winding(mesh.faces, edges):
        return None

    first_ring = tuple(mesh.vertices[index] for index in boundaries[0])
    second_ring = tuple(mesh.vertices[index] for index in boundaries[1])
    first_center = _polygon_center(first_ring)
    second_center = _polygon_center(second_ring)
    axis = _unit(_sub(second_center, first_center))
    if axis is None:
        return None
    length = _distance(first_center, second_center)
    radii = [_distance(point, first_center) for point in first_ring] + [
        _distance(point, second_center) for point in second_ring]
    radius = sum(radii) / len(radii)
    if radius <= EPSILON or length < radius * 1.5:
        return None
    plane_tol = max(length * 1.0e-5, radius * 0.005)
    for ring, level in ((first_ring, 0.0), (second_ring, length)):
        if any(abs(_dot(_sub(point, first_center), axis) - level) > plane_tol for point in ring):
            return None
    projected = tuple(_dot(_sub(point, first_center), axis) for point in mesh.vertices)
    if min(projected) < -plane_tol or max(projected) > length + plane_tol:
        return None
    guides = _guides(engine_input)
    if guides is None:
        return None
    loops, strips = guides
    if len(strips) > 1:
        return None
    reference = _sub(strips[0][0], first_center) if strips else _sub(first_ring[0], first_center)
    reference = _sub(reference, _scale(axis, _dot(reference, axis)))
    basis_u = _unit(reference)
    if basis_u is None:
        return None
    basis_v = _cross(axis, basis_u)
    circumference = 2.0 * pi * radius
    target = settings.target_quad_count
    segments = max(4, round(sqrt(target * circumference / length)))
    intervals = max(1, round(target / segments))
    if segments * intervals > MAX_OUTPUT_QUADS or abs(segments * intervals - target) / target > 0.05:
        return None
    anchors = {0: 0.0, intervals: length}
    for loop in loops:
        center = _polygon_center(loop)
        level = _dot(_sub(center, first_center), axis)
        if level < 0.0 or level > length:
            return None
        if any(abs(_dot(_sub(point, first_center), axis) - level) > radius * 0.1 for point in loop):
            return None
        row = max(0, min(intervals, round(intervals * level / length)))
        if row in anchors and abs(anchors[row] - level) > plane_tol:
            return None
        anchors[row] = level
    sorted_anchors = sorted(anchors.items())
    if any(a[1] >= b[1] for a, b in zip(sorted_anchors, sorted_anchors[1:])):
        return None
    levels = [0.0] * (intervals + 1)
    for (a_index, a_level), (b_index, b_level) in zip(sorted_anchors, sorted_anchors[1:]):
        for index in range(a_index, b_index + 1):
            levels[index] = a_level + (b_level - a_level) * (index - a_index) / (b_index - a_index)

    triangles = sorted(
        ((min(projected[index] for index in face),
          max(projected[index] for index in face), face) for face in mesh.faces),
        key=lambda item: item[0],
    )
    rows: list[tuple[Vector3, ...]] = []
    active: list[tuple[float, tuple[int, int, int]]] = []
    cursor = 0
    for row, level in enumerate(levels):
        _check_cancelled(cancelled)
        if row == 0:
            contour = first_ring
        elif row == intervals:
            contour = second_ring
        else:
            while cursor < len(triangles) and triangles[cursor][0] <= level:
                active.append((triangles[cursor][1], triangles[cursor][2]))
                cursor += 1
            active = [item for item in active if item[0] >= level]
            contour = _slice_contour(mesh.vertices, projected, active, level, basis_u, basis_v, radius)
            if contour is None:
                return None
        center = _polygon_center(contour)
        expected_center = _mix(first_center, second_center, level / length)
        if _distance(center, expected_center) > radius * 0.8:
            return None
        polar = _polar_contour(contour, center, basis_u, basis_v)
        if polar is None:
            return None
        angles, sorted_points = polar
        points: list[Vector3] = []
        for column in range(segments):
            _check_cancelled(cancelled)
            angle = 2.0 * pi * column / segments
            direction = _add(_scale(basis_u, cos(angle)), _scale(basis_v, sin(angle)))
            edge_end = bisect_right(angles, angle) % len(angles)
            edge_start = (edge_end - 1) % len(angles)
            point = _ray_segment(center, direction, axis,
                                 sorted_points[edge_start], sorted_points[edge_end])
            if point is None:
                return None
            points.append(point)
        rows.append(tuple(points))
    tolerance = max(radius * 0.08, length * 0.01)
    for loop in loops:
        level = _dot(_sub(_polygon_center(loop), first_center), axis)
        index = min(range(len(levels)), key=lambda item: abs(levels[item] - level))
        if _closed_distance(loop, rows[index]) > tolerance:
            return None
    if strips:
        column = tuple(row[0] for row in rows)
        if _open_distance(strips[0], column) > tolerance:
            return None
        if (min(_distance(strips[0][0], column[0]), _distance(strips[0][0], column[-1])) > tolerance or
                min(_distance(strips[0][-1], column[0]), _distance(strips[0][-1], column[-1])) > tolerance):
            return None
    orientation = _orientation(mesh, first_center, second_center, axis)
    if orientation == 0:
        return None
    faces: list[tuple[int, int, int, int]] = []
    for row in range(intervals):
        for column in range(segments):
            a, b = row * segments + column, row * segments + (column + 1) % segments
            c, d = (row + 1) * segments + (column + 1) % segments, (row + 1) * segments + column
            faces.append((a, b, c, d) if orientation > 0 else (a, d, c, b))
    result = MeshData(tuple(point for row in rows for point in row), tuple(faces))
    report = validate_layout(result, LayoutExpectations(
        boundary_edge_count=segments * 2,
        max_face_aspect_ratio=5.0,
        max_edge_spacing_cv=0.20,
    ))
    return result if report.ok else None


def _slice_contour(vertices: tuple[Vector3, ...], projected: tuple[float, ...],
                   active: list[tuple[float, tuple[int, int, int]]], level: float,
                   basis_u: Vector3, basis_v: Vector3, radius: float) -> tuple[Vector3, ...] | None:
    tolerance = max(radius * 1.0e-7, 1.0e-9)
    positions: dict[tuple[int, int], Vector3] = {}
    links: dict[tuple[int, int], set[tuple[int, int]]] = {}

    def key(point: Vector3) -> tuple[int, int]:
        return (round(_dot(point, basis_u) / tolerance), round(_dot(point, basis_v) / tolerance))

    for _, face in active:
        hits: dict[tuple[int, int], Vector3] = {}
        for first, second in zip(face, (face[1], face[2], face[0])):
            a, b = projected[first] - level, projected[second] - level
            if a * b > 0 or abs(a - b) <= EPSILON:
                continue
            fraction = a / (a - b)
            if -EPSILON <= fraction <= 1.0 + EPSILON:
                point = _mix(vertices[first], vertices[second], fraction)
                hits[key(point)] = point
        if len(hits) != 2:
            continue
        first, second = tuple(hits)
        if first == second:
            continue
        positions.update(hits)
        links.setdefault(first, set()).add(second)
        links.setdefault(second, set()).add(first)
    if len(links) < 4 or any(len(neighbors) != 2 for neighbors in links.values()):
        return None
    start = min(links)
    order = [start]
    previous = None
    current = start
    while True:
        choices = links[current] - ({previous} if previous is not None else set())
        if not choices:
            return None
        next_key = min(choices)
        if next_key == start:
            break
        if next_key in order:
            return None
        order.append(next_key)
        previous, current = current, next_key
    if len(order) != len(links):
        return None
    return tuple(positions[item] for item in order)


def _polar_contour(contour: tuple[Vector3, ...], center: Vector3,
                   basis_u: Vector3, basis_v: Vector3
                   ) -> tuple[tuple[float, ...], tuple[Vector3, ...]] | None:
    """방사 방향 정렬이 실제 폐곡선 순서와 같을 때만 빠른 교차표를 만든다."""
    indexed = sorted(
        ((atan2(_dot(_sub(point, center), basis_v),
                _dot(_sub(point, center), basis_u)) % (2.0 * pi), index, point)
         for index, point in enumerate(contour)),
        key=lambda item: item[0],
    )
    count = len(indexed)
    if count < 4:
        return None
    direction = 0
    for index, (angle, vertex_index, _) in enumerate(indexed):
        next_angle, next_index, _ = indexed[(index + 1) % count]
        gap = (next_angle - angle) % (2.0 * pi)
        if gap <= 1.0e-9 or gap >= pi:
            return None
        step = 1 if next_index == (vertex_index + 1) % count else (
            -1 if next_index == (vertex_index - 1) % count else 0)
        if step == 0 or (direction and step != direction):
            return None
        direction = step
    return tuple(item[0] for item in indexed), tuple(item[2] for item in indexed)


def _ray_segment(center: Vector3, direction: Vector3, axis: Vector3,
                 first: Vector3, second: Vector3) -> Vector3 | None:
    displacement = _sub(first, center)
    edge = _sub(second, first)
    denominator = _dot(_cross(direction, edge), axis)
    if abs(denominator) <= EPSILON:
        return None
    ray = _dot(_cross(displacement, edge), axis) / denominator
    fraction = _dot(_cross(displacement, direction), axis) / denominator
    if ray <= EPSILON or fraction < -EPSILON or fraction > 1.0 + EPSILON:
        return None
    return _mix(first, second, max(0.0, min(1.0, fraction)))


def _polygon_center(points: tuple[Vector3, ...]) -> Vector3:
    # 삼각 분할 밀도에 영향을 받지 않는 경계선 길이 가중 중심.
    total = 0.0
    accum = (0.0, 0.0, 0.0)
    for first, second in zip(points, (*points[1:], points[0])):
        length = _distance(first, second)
        total += length
        accum = _add(accum, _scale(_mix(first, second, 0.5), length))
    return _scale(accum, 1.0 / total) if total > EPSILON else _mean(points)


def _consistent_winding(faces: tuple[tuple[int, ...], ...],
                        edges: dict[tuple[int, int], list[int]]) -> bool:
    oriented = {}
    for index, face in enumerate(faces):
        for first, second in zip(face, (*face[1:], face[0])):
            key = (min(first, second), max(first, second))
            oriented[(index, key)] = first < second
    return all(len(owners) == 1 or oriented[(owners[0], edge)] != oriented[(owners[1], edge)]
               for edge, owners in edges.items())


def _orientation(mesh: MeshData, first_center: Vector3, second_center: Vector3,
                 axis: Vector3) -> int:
    score = 0.0
    for face in mesh.faces:
        a, b, c = (mesh.vertices[index] for index in face)
        normal = _cross(_sub(b, a), _sub(c, a))
        centroid = _mean((a, b, c))
        t = max(0.0, min(1.0, _dot(_sub(centroid, first_center), axis) /
                             _distance(first_center, second_center)))
        radial = _sub(centroid, _mix(first_center, second_center, t))
        score += _dot(normal, radial)
    return 1 if score > EPSILON else -1 if score < -EPSILON else 0
