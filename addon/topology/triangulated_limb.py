from __future__ import annotations

from bisect import bisect_left
from collections import deque
from math import cos, isclose, pi, sin, sqrt
from typing import Sequence

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3


EPSILON = 1.0e-9
MAX_QUADS = 200000


def try_remesh_triangulated_limb(
    engine_input: EngineInput, *, cancelled: CancelledCallback | None = None
) -> MeshData | None:
    """두 열린 끝을 가진 삼각형 튜브를 독립적인 쿼드 링으로 다시 만든다.

    원본 단면이 하나의 평면을 공유하는 링 열로 분해될 때만 처리한다. 몸통과
    팔다리가 만나는 분기점이나 단면을 확정할 수 없는 표면은 반환하지 않는다.
    """
    _check_cancelled(cancelled)
    mesh = engine_input.mesh
    settings = engine_input.settings
    if settings.symmetry_axes or mesh.hard_edges or not mesh.faces:
        return None
    if any(len(face) != 3 or len(set(face)) != 3 for face in mesh.faces):
        return None
    if settings.guide_curve_names and not engine_input.guide_curves:
        return None
    if not isclose(settings.density_scale, 1.0, abs_tol=EPSILON):
        return None
    if engine_input.density_values and max(engine_input.density_values) - min(engine_input.density_values) > EPSILON:
        return None

    edges = _edge_faces(mesh.faces)
    if any(len(owners) > 2 for owners in edges.values()):
        return None
    boundaries = _boundary_loops(tuple(edge for edge, owners in edges.items() if len(owners) == 1))
    if len(boundaries) != 2:
        return None
    if len(mesh.vertices) - len(edges) + len(mesh.faces) != 0:
        return None
    if not _connected_and_manifold(mesh, edges, boundaries):
        return None

    first_boundary = tuple(mesh.vertices[index] for index in boundaries[0])
    second_boundary = tuple(mesh.vertices[index] for index in boundaries[1])
    first_center = _mean(first_boundary)
    second_center = _mean(second_boundary)
    axis = _normal(first_boundary)
    if _dot(axis, _sub(second_center, first_center)) < 0:
        axis = _scale(axis, -1.0)
    length = _dot(_sub(second_center, first_center), axis)
    if length <= EPSILON:
        return None
    if _distance(_sub(second_center, first_center), _scale(axis, length)) > length * 0.5:
        return None

    source_rows = _source_rows(mesh, edges, first_center, axis, length, cancelled)
    if source_rows is None:
        return None
    levels, rings = source_rows
    if len(levels) < 2 or len(levels) > 10000:
        return None
    if abs(levels[0]) > length * 1.0e-5 or abs(levels[-1] - length) > length * 1.0e-5:
        return None

    guides = _guides(engine_input)
    if guides is None:
        return None
    loops, strips = guides
    if len(strips) > 1:
        return None
    reference = _sub(first_boundary[0], first_center)
    if strips:
        strip_start = strips[0][0]
        if _distance(strip_start, first_center) > _distance(strips[0][-1], first_center):
            strip_start = strips[0][-1]
        reference = _sub(strip_start, first_center)
    basis_u = _unit(_sub(reference, _scale(axis, _dot(reference, axis))))
    if basis_u is None:
        return None
    basis_v = _cross(axis, basis_u)

    centers = tuple(_mean(ring) for ring in rings)
    radius = sum(_distance(point, centers[index]) for index, ring in enumerate(rings) for point in ring) / sum(len(ring) for ring in rings)
    circumference = 2.0 * pi * radius
    target = settings.target_quad_count
    segments = max(4, round(sqrt(target * circumference / length)))
    segments = min(segments, MAX_QUADS)
    intervals = max(1, round(target / segments))
    if segments * intervals > MAX_QUADS:
        return None
    if abs(segments * intervals - target) / target > 0.05:
        return None

    anchors: dict[int, float] = {0: 0.0, intervals: length}
    for loop in loops:
        center = _mean(loop)
        level = _dot(_sub(center, first_center), axis)
        if level < -length * 0.01 or level > length * 1.01:
            return None
        index = max(0, min(intervals, round(intervals * level / length)))
        if index in anchors and abs(anchors[index] - level) > length * 1.0e-5:
            return None
        anchors[index] = level
    ordered = sorted(anchors.items())
    if any(a[1] >= b[1] for a, b in zip(ordered, ordered[1:])):
        return None
    output_levels = [0.0] * (intervals + 1)
    for (a_index, a_level), (b_index, b_level) in zip(ordered, ordered[1:]):
        for index in range(a_index, b_index + 1):
            output_levels[index] = a_level + (b_level - a_level) * (index - a_index) / (b_index - a_index)

    directions = tuple(
        _add(_scale(basis_u, cos(2.0 * pi * column / segments)),
             _scale(basis_v, sin(2.0 * pi * column / segments)))
        for column in range(segments)
    )

    def sample_ring(index: int) -> tuple[Vector3, ...] | None:
        points: list[Vector3] = []
        for direction in directions:
            _check_cancelled(cancelled)
            point = _ray_polygon(rings[index], centers[index], direction, axis)
            if point is None:
                return None
            points.append(point)
        return tuple(points)

    rows: list[tuple[Vector3, ...]] = []
    cached_index = -2
    cached_start: tuple[Vector3, ...] | None = None
    cached_end: tuple[Vector3, ...] | None = None
    for level in output_levels:
        _check_cancelled(cancelled)
        source_index = 0
        while source_index + 1 < len(levels) - 1 and levels[source_index + 1] < level:
            source_index += 1
        span = levels[source_index + 1] - levels[source_index]
        if span <= EPSILON:
            return None
        fraction = max(0.0, min(1.0, (level - levels[source_index]) / span))
        center = _mix(centers[source_index], centers[source_index + 1], fraction)
        if source_index != cached_index:
            # 출력 행이 같은 원본 단면 구간에 있으면 교점을 다시 찾지 않는다.
            cached_start = cached_end if source_index == cached_index + 1 else sample_ring(source_index)
            if cached_start is None:
                return None
            cached_end = sample_ring(source_index + 1)
            if cached_end is None:
                return None
            cached_index = source_index
        assert cached_start is not None and cached_end is not None
        points: list[Vector3] = []
        for column in range(segments):
            _check_cancelled(cancelled)
            point = _mix(cached_start[column], cached_end[column], fraction)
            if _distance(point, center) <= EPSILON:
                return None
            points.append(point)
        rows.append(tuple(points))

    tolerance = max(radius * 0.30, length * 0.02)
    for loop in loops:
        center = _mean(loop)
        level = _dot(_sub(center, first_center), axis)
        index = min(range(len(output_levels)), key=lambda row: abs(output_levels[row] - level))
        if _closed_distance(loop, rows[index]) > tolerance:
            return None
    if strips:
        strip = strips[0]
        column = tuple(row[0] for row in rows)
        if _open_distance(strip, column) > tolerance:
            return None
        if min(_distance(strip[0], column[0]), _distance(strip[0], column[-1])) > tolerance:
            return None
        if min(_distance(strip[-1], column[0]), _distance(strip[-1], column[-1])) > tolerance:
            return None

    orientation = _source_orientation(mesh, centers, levels, first_center, axis)
    if orientation == 0:
        return None
    faces: list[tuple[int, int, int, int]] = []
    for row in range(intervals):
        for column in range(segments):
            a = row * segments + column
            b = row * segments + (column + 1) % segments
            c = (row + 1) * segments + (column + 1) % segments
            d = (row + 1) * segments + column
            faces.append((a, b, c, d) if orientation > 0 else (a, d, c, b))
    return MeshData(tuple(point for row in rows for point in row), tuple(faces))


def _source_rows(mesh: MeshData, edges: dict[tuple[int, int], list[int]], origin: Vector3,
                 axis: Vector3, length: float, cancelled: CancelledCallback | None
                 ) -> tuple[tuple[float, ...], tuple[tuple[Vector3, ...], ...]] | None:
    projected = sorted((_dot(_sub(point, origin), axis), index) for index, point in enumerate(mesh.vertices))
    tolerance = max(length * 1.0e-5, EPSILON)
    groups: list[list[int]] = []
    group_levels: list[float] = []
    for level, index in projected:
        if not groups or level - group_levels[-1] > tolerance:
            groups.append([])
            group_levels.append(level)
        groups[-1].append(index)
    if len(groups) < 2:
        return None
    rings: list[tuple[Vector3, ...]] = []
    levels: list[float] = []
    neighbors: dict[int, set[int]] = {index: set() for index in range(len(mesh.vertices))}
    for first, second in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    for group in groups:
        _check_cancelled(cancelled)
        if len(group) < 4:
            return None
        group_set = set(group)
        center = _mean(tuple(mesh.vertices[index] for index in group))
        adjacency = {index: neighbors[index] & group_set for index in group}
        # 삼각 대각선은 같은 단면에 놓이면 링 차트로 확정할 수 없다.
        if any(len(neighbors) != 2 for neighbors in adjacency.values()):
            return None
        start = min(group)
        ordered = [start]
        previous = -1
        current = start
        while True:
            next_index = min(neighbor for neighbor in adjacency[current] if neighbor != previous)
            if next_index == start:
                break
            if next_index in ordered:
                return None
            ordered.append(next_index)
            previous, current = current, next_index
        if len(ordered) != len(group):
            return None
        ring = tuple(mesh.vertices[index] for index in ordered)
        if _dot(_normal(ring), axis) < 0:
            ring = tuple(reversed(ring))
        if any(_distance(point, center) <= EPSILON for point in ring):
            return None
        rings.append(ring)
        levels.append(sum(_dot(_sub(mesh.vertices[index], origin), axis) for index in group) / len(group))
    return tuple(levels), tuple(rings)


def _guides(engine_input: EngineInput) -> tuple[tuple[tuple[Vector3, ...], ...],
                                                  tuple[tuple[Vector3, ...], ...]] | None:
    loops: list[tuple[Vector3, ...]] = []
    strips: list[tuple[Vector3, ...]] = []
    for guide in engine_input.guide_curves:
        for spline, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            points = tuple(spline)
            if kind == "LOOP" and closed and len(points) >= 4:
                if _distance(points[0], points[-1]) <= EPSILON:
                    points = points[:-1]
                loops.append(points)
            elif kind == "STRIP" and not closed and len(points) >= 2:
                strips.append(points)
            else:
                return None
    return tuple(loops), tuple(strips)


def _source_orientation(mesh: MeshData, centers: Sequence[Vector3], levels: Sequence[float],
                        origin: Vector3, axis: Vector3) -> int:
    score = 0.0
    for face in mesh.faces:
        a, b, c = (mesh.vertices[index] for index in face)
        normal = _cross(_sub(b, a), _sub(c, a))
        centroid = _mean((a, b, c))
        level = _dot(_sub(centroid, origin), axis)
        insertion = bisect_left(levels, level)
        row = min((max(0, insertion - 1), min(len(levels) - 1, insertion)),
                  key=lambda index: abs(levels[index] - level))
        radial = _sub(centroid, centers[row])
        score += _dot(normal, radial)
    if abs(score) <= EPSILON:
        return 0
    # 링 순서는 축 양의 방향이다. (a,b,c,d)의 법선은 바깥 방향이다.
    return 1 if score > 0 else -1


def _connected_and_manifold(mesh: MeshData, edges: dict[tuple[int, int], list[int]],
                            boundaries: Sequence[Sequence[int]]) -> bool:
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(mesh.vertices))}
    incident_faces: dict[int, list[tuple[int, ...]]] = {index: [] for index in range(len(mesh.vertices))}
    for face in mesh.faces:
        for index in face:
            incident_faces[index].append(face)
    for first, second in edges:
        adjacency[first].add(second)
        adjacency[second].add(first)
    seen = {0}
    queue = deque((0,))
    while queue:
        for neighbor in adjacency[queue.popleft()]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    if len(seen) != len(mesh.vertices):
        return False
    boundary_vertices = {index for loop in boundaries for index in loop}
    for vertex, neighbors in adjacency.items():
        incident = incident_faces[vertex]
        links: dict[int, set[int]] = {neighbor: set() for neighbor in neighbors}
        for face in incident:
            other = [index for index in face if index != vertex]
            if len(other) != 2:
                return False
            links[other[0]].add(other[1])
            links[other[1]].add(other[0])
        degrees = sorted(len(value) for value in links.values())
        expected = [1, 1] + [2] * (len(links) - 2) if vertex in boundary_vertices else [2] * len(links)
        if degrees != expected:
            return False
        reached = {next(iter(links))}
        pending = list(reached)
        while pending:
            for neighbor in links[pending.pop()]:
                if neighbor not in reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
        if len(reached) != len(links):
            return False
    return True


def _edge_faces(faces: Sequence[Sequence[int]]) -> dict[tuple[int, int], list[int]]:
    result: dict[tuple[int, int], list[int]] = {}
    for face_index, face in enumerate(faces):
        for first, second in zip(face, (*face[1:], face[0])):
            edge = (min(first, second), max(first, second))
            result.setdefault(edge, []).append(face_index)
    return result


def _boundary_loops(edges: Sequence[tuple[int, int]]) -> tuple[tuple[int, ...], ...]:
    adjacency: dict[int, set[int]] = {}
    for first, second in edges:
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
    if any(len(neighbors) != 2 for neighbors in adjacency.values()):
        return ()
    loops: list[tuple[int, ...]] = []
    remaining = set(adjacency)
    while remaining:
        start = min(remaining)
        loop = [start]
        previous = -1
        current = start
        while True:
            choices = adjacency[current] - {previous}
            if len(choices) != 1 and previous != -1:
                return ()
            next_index = min(choices)
            if next_index == start:
                break
            if next_index in loop:
                return ()
            loop.append(next_index)
            previous, current = current, next_index
        if len(loop) < 4:
            return ()
        remaining.difference_update(loop)
        loops.append(tuple(loop))
    return tuple(loops)


def _ray_polygon(ring: Sequence[Vector3], center: Vector3, direction: Vector3,
                 axis: Vector3) -> Vector3 | None:
    best: tuple[float, Vector3] | None = None
    for first, second in zip(ring, (*ring[1:], ring[0])):
        a = _sub(first, center)
        edge = _sub(second, first)
        denominator = _dot(_cross(direction, edge), axis)
        if abs(denominator) <= EPSILON:
            continue
        ray = _dot(_cross(a, edge), axis) / denominator
        segment = _dot(_cross(a, direction), axis) / denominator
        if ray < -EPSILON or segment < -EPSILON or segment > 1.0 + EPSILON:
            continue
        point = _mix(first, second, max(0.0, min(1.0, segment)))
        if best is None or ray < best[0]:
            best = (ray, point)
    return None if best is None else best[1]


def _closed_distance(first: Sequence[Vector3], second: Sequence[Vector3]) -> float:
    return max(
        max(_point_line_distance(point, second, closed=True) for point in first),
        max(_point_line_distance(point, first, closed=True) for point in second),
    )


def _open_distance(first: Sequence[Vector3], second: Sequence[Vector3]) -> float:
    return max(
        max(_point_line_distance(point, second, closed=False) for point in first),
        max(_point_line_distance(point, first, closed=False) for point in second),
    )


def _point_line_distance(point: Vector3, line: Sequence[Vector3], *, closed: bool) -> float:
    end = (*line[1:], line[0]) if closed else line[1:]
    return min(_distance(point, _closest_segment(point, first, second)) for first, second in zip(line, end))


def _closest_segment(point: Vector3, start: Vector3, end: Vector3) -> Vector3:
    segment = _sub(end, start)
    square = _dot(segment, segment)
    if square <= EPSILON:
        return start
    amount = max(0.0, min(1.0, _dot(_sub(point, start), segment) / square))
    return _mix(start, end, amount)


def _normal(points: Sequence[Vector3]) -> Vector3:
    value = (0.0, 0.0, 0.0)
    for first, second in zip(points, (*points[1:], points[0])):
        value = _add(value, _cross(first, second))
    return _unit(value) or (0.0, 0.0, 0.0)


def _mean(points: Sequence[Vector3]) -> Vector3:
    count = len(points)
    return tuple(sum(point[axis] for point in points) / count for axis in range(3))  # type: ignore[return-value]


def _mix(first: Vector3, second: Vector3, amount: float) -> Vector3:
    return _add(_scale(first, 1.0 - amount), _scale(second, amount))


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _add(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] + second[0], first[1] + second[1], first[2] + second[2])


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _dot(first: Vector3, second: Vector3) -> float:
    return sum(a * b for a, b in zip(first, second))


def _cross(first: Vector3, second: Vector3) -> Vector3:
    return (first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0])


def _distance(first: Vector3, second: Vector3) -> float:
    return sqrt(_dot(_sub(first, second), _sub(first, second)))


def _unit(vector: Vector3) -> Vector3 | None:
    length = sqrt(_dot(vector, vector))
    return None if length <= EPSILON else _scale(vector, 1.0 / length)


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("리메시 작업이 취소되었습니다.")
