from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import atan2, cos, isclose, sin, sqrt
from typing import Sequence

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3


EPSILON = 1.0e-9
MAX_PERIODIC_QUADS = 200000


@dataclass(frozen=True)
class _TubeChart:
    layers: tuple[tuple[int, ...], ...]
    reverse_faces: bool


@dataclass(frozen=True)
class _GuideRow:
    output_index: int
    distance: float


@dataclass(frozen=True)
class _GuideColumn:
    segment: int
    spline: tuple[Vector3, ...]


def try_remesh_periodic(engine_input: EngineInput, *, cancelled: CancelledCallback | None = None) -> MeshData | None:
    """균일 간격 주기 튜브 차트를 반환하거나, 지원하지 않는 형상에는 None을 반환한다.

    지원 형상은 두 개의 닫힌 경계 루프와 링 방향 주기를 가진 연결된 올쿼드
    다양체 튜브다. LOOP 가이드는 단면 둘레에 명확히 대응할 때 출력 row로,
    STRIP 가이드는 양끝 경계까지 이어지는 한 길이 방향 열에 대응할 때 출력
    column으로 고정한다. DIRECTION, 대칭, 밀도, 명시적 하드엣지는 거절한다.
    """
    _check_cancelled(cancelled)
    if not _supports_controls(engine_input):
        return None

    mesh = engine_input.mesh
    if any(len(face) != 4 for face in mesh.faces):
        return None

    chart = _extract_bounded_tube_chart(mesh)
    if chart is None:
        return None

    ring_count = len(chart.layers[0])
    longitudinal_count = _choose_longitudinal_count(ring_count, engine_input.settings.target_quad_count)
    if ring_count * longitudinal_count > MAX_PERIODIC_QUADS:
        longitudinal_count = max(1, MAX_PERIODIC_QUADS // ring_count)
    if longitudinal_count < 1:
        return None

    guides = _classified_guide_splines(engine_input)
    if guides is None:
        return None
    loop_splines, strip_splines = guides

    rows, guide_rows = _resample_longitudinal_rows(
        mesh.vertices,
        chart.layers,
        longitudinal_count,
        ring_count,
        loop_splines,
        cancelled,
    )
    if rows is None:
        return None
    guide_column = _match_strip_guides(rows, strip_splines)
    if guide_column is None:
        return None
    if guide_column is not False:
        rows = _rotate_rows(rows, guide_column.segment)
        rows = _snap_first_column_to_strip(rows, guide_column.spline)
        guide_column = _GuideColumn(segment=0, spline=guide_column.spline)

    vertices = tuple(vertex for row in rows for vertex in row)
    faces: list[tuple[int, int, int, int]] = []
    for row_index in range(longitudinal_count):
        _check_cancelled(cancelled)
        row_start = row_index * ring_count
        next_start = (row_index + 1) * ring_count
        for segment in range(ring_count):
            next_segment = (segment + 1) % ring_count
            if chart.reverse_faces:
                faces.append(
                    (
                        row_start + segment,
                        next_start + segment,
                        next_start + next_segment,
                        row_start + next_segment,
                    )
                )
            else:
                faces.append(
                    (
                        row_start + segment,
                        row_start + next_segment,
                        next_start + next_segment,
                        next_start + segment,
                    )
                )

    result = MeshData(vertices=vertices, faces=tuple(faces))
    if not all(_has_closed_output_ring(result, guide_row.output_index, ring_count) for guide_row in guide_rows):
        return None
    if guide_column is not False and not _has_longitudinal_output_chain(
        result,
        guide_column.segment,
        longitudinal_count,
        ring_count,
    ):
        return None
    return result


def _supports_controls(engine_input: EngineInput) -> bool:
    settings = engine_input.settings
    if settings.symmetry_axes:
        return False
    if settings.guide_curve_names and not engine_input.guide_curves:
        return False
    if not _density_values_are_uniform(engine_input.density_values):
        return False
    if not isclose(settings.density_scale, 1.0, rel_tol=0.0, abs_tol=EPSILON):
        return False
    if engine_input.mesh.hard_edges:
        return False
    return True


def _density_values_are_uniform(values: Sequence[float]) -> bool:
    if not values:
        return True
    first = values[0]
    return all(isclose(value, first, rel_tol=0.0, abs_tol=EPSILON) for value in values[1:])


def _classified_guide_splines(
    engine_input: EngineInput,
) -> tuple[tuple[tuple[Vector3, ...], ...], tuple[tuple[Vector3, ...], ...]] | None:
    loop_splines: list[tuple[Vector3, ...]] = []
    strip_splines: list[tuple[Vector3, ...]] = []
    for guide in engine_input.guide_curves:
        for spline, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            if kind == "LOOP" and closed:
                points = _trim_closed_points(spline)
                if len(points) < 4:
                    return None
                loop_splines.append(points)
                continue
            if kind == "STRIP" and not closed:
                points = tuple(spline)
                if len(points) < 2:
                    return None
                strip_splines.append(points)
                continue
            if kind == "DIRECTION":
                return None
            return None
    return tuple(loop_splines), tuple(strip_splines)


def _trim_closed_points(points: Sequence[Vector3]) -> tuple[Vector3, ...]:
    trimmed = tuple(points)
    if len(trimmed) >= 2 and _distance(trimmed[0], trimmed[-1]) <= EPSILON:
        trimmed = trimmed[:-1]
    return trimmed


def _extract_bounded_tube_chart(mesh: MeshData) -> _TubeChart | None:
    edge_faces = _edge_faces(mesh.faces)
    if any(len(face_indices) > 2 for face_indices in edge_faces.values()):
        return None

    boundary_edges = tuple(edge for edge, face_indices in edge_faces.items() if len(face_indices) == 1)
    loops = _boundary_loops(boundary_edges)
    if len(loops) != 2:
        return None
    if len(loops[0]) < 4 or len(loops[0]) != len(loops[1]):
        return None

    neighbors = _vertex_neighbors(mesh.faces)
    start_loop = loops[0]
    end_loop = loops[1]
    distances = _distances_from(tuple(start_loop), neighbors)
    if len(distances) != len(mesh.vertices):
        return None
    end_distances = {distances.get(vertex) for vertex in end_loop}
    if len(end_distances) != 1:
        return None
    maximum_distance = end_distances.pop()
    if maximum_distance is None or maximum_distance < 1:
        return None
    if any(distance < 0 or distance > maximum_distance for distance in distances.values()):
        return None

    layer_sets = [set() for _ in range(maximum_distance + 1)]
    for vertex, distance in distances.items():
        layer_sets[distance].add(vertex)
    ring_size = len(start_loop)
    if any(len(layer) != ring_size for layer in layer_sets):
        return None

    ordered_layers: list[tuple[int, ...]] = [tuple(start_loop)]
    for distance in range(1, maximum_distance + 1):
        previous = ordered_layers[-1]
        current: list[int] = []
        for vertex in previous:
            candidates = [neighbor for neighbor in neighbors[vertex] if distances.get(neighbor) == distance]
            if len(candidates) != 1:
                return None
            current.append(candidates[0])
        if set(current) != layer_sets[distance]:
            return None
        if not _is_ordered_ring(tuple(current), neighbors):
            return None
        ordered_layers.append(tuple(current))

    reverse_faces = _reverse_face_order(mesh, tuple(ordered_layers))
    if reverse_faces is None:
        return None
    return _TubeChart(layers=tuple(ordered_layers), reverse_faces=reverse_faces)


def _edge_faces(faces: Sequence[Sequence[int]]) -> dict[tuple[int, int], tuple[int, ...]]:
    pending: dict[tuple[int, int], list[int]] = {}
    for face_index, face in enumerate(faces):
        for edge in _face_edges(face):
            pending.setdefault(edge, []).append(face_index)
    return {edge: tuple(indices) for edge, indices in pending.items()}


def _face_edges(face: Sequence[int]) -> tuple[tuple[int, int], ...]:
    return tuple(_edge_key(first, second) for first, second in zip(face, (*face[1:], face[0])))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _boundary_loops(boundary_edges: Sequence[tuple[int, int]]) -> tuple[tuple[int, ...], ...]:
    adjacency: dict[int, set[int]] = {}
    for first, second in boundary_edges:
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
    if any(len(neighbors) != 2 for neighbors in adjacency.values()):
        return ()

    loops: list[tuple[int, ...]] = []
    remaining = set(adjacency)
    while remaining:
        start = min(remaining)
        previous = -1
        current = start
        loop: list[int] = []
        while True:
            loop.append(current)
            remaining.discard(current)
            candidates = sorted(neighbor for neighbor in adjacency[current] if neighbor != previous)
            if not candidates:
                return ()
            next_vertex = candidates[0]
            if next_vertex == start:
                break
            previous, current = current, next_vertex
            if current in loop:
                return ()
        if len(loop) < 3:
            return ()
        loops.append(tuple(loop))
    return tuple(sorted(loops, key=lambda item: (len(item), item)))


def _vertex_neighbors(faces: Sequence[Sequence[int]]) -> dict[int, set[int]]:
    neighbors: dict[int, set[int]] = {}
    for face in faces:
        for first, second in zip(face, (*face[1:], face[0])):
            neighbors.setdefault(first, set()).add(second)
            neighbors.setdefault(second, set()).add(first)
    return neighbors


def _distances_from(starts: Sequence[int], neighbors: dict[int, set[int]]) -> dict[int, int]:
    distances = {vertex: 0 for vertex in starts}
    queue: deque[int] = deque(starts)
    while queue:
        vertex = queue.popleft()
        for neighbor in neighbors[vertex]:
            if neighbor in distances:
                continue
            distances[neighbor] = distances[vertex] + 1
            queue.append(neighbor)
    return distances


def _is_ordered_ring(layer: Sequence[int], neighbors: dict[int, set[int]]) -> bool:
    if len(set(layer)) != len(layer):
        return False
    for index, vertex in enumerate(layer):
        if layer[(index + 1) % len(layer)] not in neighbors[vertex]:
            return False
    return True


def _reverse_face_order(mesh: MeshData, layers: tuple[tuple[int, ...], ...]) -> bool | None:
    face_by_vertices = {frozenset(face): face for face in mesh.faces}
    orientation_score = 0.0
    ring_size = len(layers[0])
    for row_index in range(len(layers) - 1):
        for segment in range(ring_size):
            candidate = (
                layers[row_index][segment],
                layers[row_index][(segment + 1) % ring_size],
                layers[row_index + 1][(segment + 1) % ring_size],
                layers[row_index + 1][segment],
            )
            source_face = face_by_vertices.get(frozenset(candidate))
            if source_face is None:
                return None
            source_normal = _polygon_normal(mesh.vertices, source_face)
            candidate_normal = _polygon_normal(mesh.vertices, candidate)
            orientation_score += _dot(source_normal, candidate_normal)
    return orientation_score < 0.0


def _choose_longitudinal_count(ring_count: int, target_quad_count: int) -> int:
    return max(1, min(MAX_PERIODIC_QUADS // ring_count, round(target_quad_count / ring_count)))


def _resample_longitudinal_rows(
    vertices: Sequence[Vector3],
    layers: tuple[tuple[int, ...], ...],
    longitudinal_count: int,
    ring_count: int,
    guide_splines: Sequence[Sequence[Vector3]],
    cancelled: CancelledCallback | None,
) -> tuple[tuple[tuple[Vector3, ...], ...] | None, tuple[_GuideRow, ...]]:
    source_rows = tuple(tuple(vertices[index] for index in layer) for layer in layers)
    center_distances = _centerline_distances(source_rows)
    total_length = center_distances[-1]
    if total_length <= EPSILON:
        center_distances = tuple(float(index) for index in range(len(source_rows)))
        total_length = center_distances[-1]
    if total_length <= EPSILON:
        return None, ()
    guide_rows = _match_loop_guides(
        source_rows,
        center_distances,
        total_length,
        longitudinal_count,
        guide_splines,
    )
    if guide_rows is None:
        return None, ()
    row_distances = _output_row_distances(total_length, longitudinal_count, guide_rows)
    if row_distances is None:
        return None, ()

    rows: list[tuple[Vector3, ...]] = []
    source_index = 0
    for target_distance in row_distances:
        _check_cancelled(cancelled)
        while (
            source_index + 1 < len(center_distances) - 1
            and center_distances[source_index + 1] < target_distance
        ):
            source_index += 1
        start_distance = center_distances[source_index]
        end_distance = center_distances[source_index + 1]
        if end_distance - start_distance <= EPSILON:
            alpha = 0.0
        else:
            alpha = (target_distance - start_distance) / (end_distance - start_distance)
        row = tuple(
            _lerp(source_rows[source_index][segment], source_rows[source_index + 1][segment], alpha)
            for segment in range(ring_count)
        )
        resampled = _resample_closed_row(row, ring_count)
        if resampled is None:
            return None, ()
        rows.append(resampled)
    return tuple(rows), guide_rows


def _match_loop_guides(
    source_rows: Sequence[Sequence[Vector3]],
    center_distances: Sequence[float],
    total_length: float,
    longitudinal_count: int,
    guide_splines: Sequence[Sequence[Vector3]],
) -> tuple[_GuideRow, ...] | None:
    if not guide_splines:
        return ()
    tolerance = _guide_tolerance(source_rows)
    centers = tuple(_cross_section_center(row) for row in source_rows)
    guide_rows: list[_GuideRow] = []
    for spline in guide_splines:
        guide_center = _centroid(spline)
        distance = _project_point_to_centerline_distance(guide_center, centers, center_distances, tolerance)
        if distance is None:
            return None
        row = _source_row_at_distance(source_rows, center_distances, distance)
        if row is None:
            return None
        resampled_row = _resample_closed_row(row, len(spline))
        if resampled_row is None:
            return None
        if not _closed_loops_match(spline, resampled_row, tolerance):
            return None
        output_index = round(longitudinal_count * distance / total_length)
        output_index = max(0, min(longitudinal_count, output_index))
        guide_rows.append(_GuideRow(output_index=output_index, distance=distance))
    return tuple(guide_rows)


def _output_row_distances(
    total_length: float,
    longitudinal_count: int,
    guide_rows: Sequence[_GuideRow],
) -> tuple[float, ...] | None:
    anchors: dict[int, float] = {0: 0.0, longitudinal_count: total_length}
    tolerance = max(total_length * 1.0e-6, EPSILON)
    for guide_row in guide_rows:
        existing = anchors.get(guide_row.output_index)
        if existing is not None and abs(existing - guide_row.distance) > tolerance:
            return None
        anchors[guide_row.output_index] = guide_row.distance

    ordered = sorted(anchors.items())
    if any(left[0] == right[0] or left[1] >= right[1] for left, right in zip(ordered, ordered[1:])):
        return None

    distances = [0.0] * (longitudinal_count + 1)
    for (start_index, start_distance), (end_index, end_distance) in zip(ordered, ordered[1:]):
        span = end_index - start_index
        if span <= 0:
            return None
        for index in range(start_index, end_index + 1):
            alpha = (index - start_index) / span
            distances[index] = start_distance + (end_distance - start_distance) * alpha
    return tuple(distances)


def _source_row_at_distance(
    source_rows: Sequence[Sequence[Vector3]],
    center_distances: Sequence[float],
    target_distance: float,
) -> tuple[Vector3, ...] | None:
    source_index = 0
    while (
        source_index + 1 < len(center_distances) - 1
        and center_distances[source_index + 1] < target_distance
    ):
        source_index += 1
    start_distance = center_distances[source_index]
    end_distance = center_distances[source_index + 1]
    if end_distance - start_distance <= EPSILON:
        alpha = 0.0
    else:
        alpha = (target_distance - start_distance) / (end_distance - start_distance)
    return tuple(
        _lerp(source_rows[source_index][segment], source_rows[source_index + 1][segment], alpha)
        for segment in range(len(source_rows[0]))
    )


def _project_point_to_centerline_distance(
    point: Vector3,
    centers: Sequence[Vector3],
    center_distances: Sequence[float],
    tolerance: float,
) -> float | None:
    matches: list[tuple[float, float]] = []
    for index, (start, end) in enumerate(zip(centers, centers[1:])):
        segment = _sub(end, start)
        length_squared = _dot(segment, segment)
        if length_squared <= EPSILON:
            continue
        alpha = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
        projected = _lerp(start, end, alpha)
        distance_to_segment = _distance(point, projected)
        centerline_distance = center_distances[index] + alpha * (center_distances[index + 1] - center_distances[index])
        matches.append((distance_to_segment, centerline_distance))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    if matches[0][0] > tolerance:
        return None
    close = [item for item in matches if item[0] <= matches[0][0] + tolerance]
    if max(item[1] for item in close) - min(item[1] for item in close) > tolerance:
        return None
    return sum(item[1] for item in close) / len(close)


def _closed_loops_match(first: Sequence[Vector3], second: Sequence[Vector3], tolerance: float) -> bool:
    first_to_second = max(_point_to_closed_polyline_distance(point, second) for point in first)
    second_to_first = max(_point_to_closed_polyline_distance(point, first) for point in second)
    return max(first_to_second, second_to_first) <= tolerance


def _point_to_closed_polyline_distance(point: Vector3, loop: Sequence[Vector3]) -> float:
    return min(
        _point_to_segment_distance(point, first, second)
        for first, second in zip(loop, (*loop[1:], loop[0]))
    )


def _point_to_segment_distance(point: Vector3, start: Vector3, end: Vector3) -> float:
    segment = _sub(end, start)
    length_squared = _dot(segment, segment)
    if length_squared <= EPSILON:
        return _distance(point, start)
    alpha = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
    return _distance(point, _lerp(start, end, alpha))


def _guide_tolerance(rows: Sequence[Sequence[Vector3]]) -> float:
    points = tuple(point for row in rows for point in row)
    minimum = tuple(min(point[axis] for point in points) for axis in range(3))
    maximum = tuple(max(point[axis] for point in points) for axis in range(3))
    diagonal = _distance(minimum, maximum)
    return max(diagonal * 0.03, 1.0e-6)


def _has_closed_output_ring(mesh: MeshData, row: int, ring_count: int) -> bool:
    edges = set()
    for face in mesh.faces:
        edges.update(_face_edges(face))
    offset = row * ring_count
    return all(
        _edge_key(offset + segment, offset + (segment + 1) % ring_count) in edges
        for segment in range(ring_count)
    )


def _match_strip_guides(
    rows: Sequence[Sequence[Vector3]],
    guide_splines: Sequence[Sequence[Vector3]],
) -> _GuideColumn | None | bool:
    if not guide_splines:
        return False
    tolerance = _guide_tolerance(rows)
    columns = _longitudinal_columns(rows)
    matched: list[tuple[int, tuple[Vector3, ...]]] = []
    for spline in guide_splines:
        column = _match_one_strip_guide(columns, spline, tolerance)
        if column is None:
            return None
        matched.append((column, tuple(spline)))
    if len({column for column, _ in matched}) != 1:
        return None
    return _GuideColumn(segment=matched[0][0], spline=matched[0][1])


def _longitudinal_columns(rows: Sequence[Sequence[Vector3]]) -> tuple[tuple[Vector3, ...], ...]:
    ring_count = len(rows[0])
    return tuple(tuple(row[segment] for row in rows) for segment in range(ring_count))


def _match_one_strip_guide(
    columns: Sequence[Sequence[Vector3]],
    spline: Sequence[Vector3],
    tolerance: float,
) -> int | None:
    scores: list[tuple[float, int]] = []
    for column_index, column in enumerate(columns):
        if not _strip_reaches_column_boundaries(spline, column, tolerance):
            continue
        guide_to_column = max(_point_to_polyline_distance(point, column) for point in spline)
        column_to_guide = max(_point_to_polyline_distance(point, spline) for point in column)
        score = max(guide_to_column, column_to_guide)
        if score <= tolerance:
            scores.append((score, column_index))
    if not scores:
        return None
    scores.sort()
    close = [item for item in scores if item[0] <= scores[0][0] + tolerance]
    if len({item[1] for item in close}) != 1:
        return None
    return scores[0][1]


def _strip_reaches_column_boundaries(
    spline: Sequence[Vector3],
    column: Sequence[Vector3],
    tolerance: float,
) -> bool:
    endpoints = (column[0], column[-1])
    guide_endpoints = (spline[0], spline[-1])
    direct = max(_distance(a, b) for a, b in zip(guide_endpoints, endpoints))
    reverse = max(_distance(a, b) for a, b in zip(reversed(guide_endpoints), endpoints))
    return min(direct, reverse) <= tolerance


def _point_to_polyline_distance(point: Vector3, line: Sequence[Vector3]) -> float:
    return min(
        _point_to_segment_distance(point, first, second)
        for first, second in zip(line, line[1:])
    )


def _closest_point_on_polyline(point: Vector3, line: Sequence[Vector3]) -> Vector3:
    best_distance = float("inf")
    best_point = line[0]
    for start, end in zip(line, line[1:]):
        segment = _sub(end, start)
        length_squared = _dot(segment, segment)
        if length_squared <= EPSILON:
            candidate = start
        else:
            alpha = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
            candidate = _lerp(start, end, alpha)
        distance = _distance(point, candidate)
        if distance < best_distance:
            best_distance = distance
            best_point = candidate
    return best_point


def _rotate_rows(
    rows: tuple[tuple[Vector3, ...], ...],
    segment: int,
) -> tuple[tuple[Vector3, ...], ...]:
    if segment == 0:
        return rows
    return tuple(tuple((*row[segment:], *row[:segment])) for row in rows)


def _snap_first_column_to_strip(
    rows: tuple[tuple[Vector3, ...], ...],
    spline: Sequence[Vector3],
) -> tuple[tuple[Vector3, ...], ...]:
    snapped_rows: list[tuple[Vector3, ...]] = []
    for row in rows:
        snapped_rows.append((_closest_point_on_polyline(row[0], spline), *row[1:]))
    return tuple(snapped_rows)


def _has_longitudinal_output_chain(
    mesh: MeshData,
    segment: int,
    longitudinal_count: int,
    ring_count: int,
) -> bool:
    edges = set()
    for face in mesh.faces:
        edges.update(_face_edges(face))
    return all(
        _edge_key(row * ring_count + segment, (row + 1) * ring_count + segment) in edges
        for row in range(longitudinal_count)
    )


def _centerline_distances(rows: Sequence[Sequence[Vector3]]) -> tuple[float, ...]:
    distances = [0.0]
    previous = _cross_section_center(rows[0])
    total = 0.0
    for row in rows[1:]:
        center = _cross_section_center(row)
        total += _distance(previous, center)
        distances.append(total)
        previous = center
    return tuple(distances)


def _cross_section_center(row: Sequence[Vector3]) -> Vector3:
    fit = _fit_near_circular_row(row)
    if fit is None:
        return _centroid(row)
    return fit[0]


def _resample_closed_row(row: Sequence[Vector3], count: int) -> tuple[Vector3, ...] | None:
    circular = _resample_near_circular_row(row, count)
    if circular is not None:
        return circular

    edge_lengths = [_distance(row[index], row[(index + 1) % len(row)]) for index in range(len(row))]
    perimeter = sum(edge_lengths)
    if perimeter <= EPSILON:
        return None

    samples: list[Vector3] = []
    edge_index = 0
    accumulated = 0.0
    for sample_index in range(count):
        target = perimeter * sample_index / count
        while edge_index + 1 < len(edge_lengths) and accumulated + edge_lengths[edge_index] < target:
            accumulated += edge_lengths[edge_index]
            edge_index += 1
        edge_length = edge_lengths[edge_index]
        alpha = 0.0 if edge_length <= EPSILON else (target - accumulated) / edge_length
        samples.append(_lerp(row[edge_index], row[(edge_index + 1) % len(row)], alpha))
    return tuple(samples)


def _resample_near_circular_row(row: Sequence[Vector3], count: int) -> tuple[Vector3, ...] | None:
    fit = _fit_near_circular_row(row)
    if fit is None:
        return None
    center, first_axis, second_axis, radius, winding = fit

    return tuple(
        _add(
            center,
            _scale(
                _add(
                    _scale(first_axis, cos(winding * 6.283185307179586 * index / count)),
                    _scale(second_axis, sin(winding * 6.283185307179586 * index / count)),
                ),
                radius,
            ),
        )
        for index in range(count)
    )


def _fit_near_circular_row(row: Sequence[Vector3]) -> tuple[Vector3, Vector3, Vector3, float, float] | None:
    normal = _polygon_normal_from_points(row)
    normal_length = _length(normal)
    if normal_length <= EPSILON:
        return None
    normal = _scale(normal, 1.0 / normal_length)
    provisional_center = _centroid(row)
    first_axis = _sub(row[0], provisional_center)
    first_axis_length = _length(first_axis)
    if first_axis_length <= EPSILON:
        return None
    first_axis = _scale(first_axis, 1.0 / first_axis_length)
    second_axis = _cross(normal, first_axis)
    second_axis_length = _length(second_axis)
    if second_axis_length <= EPSILON:
        return None
    second_axis = _scale(second_axis, 1.0 / second_axis_length)

    projected = tuple(
        (_dot(_sub(point, provisional_center), first_axis), _dot(_sub(point, provisional_center), second_axis))
        for point in row
    )
    circle = _fit_circle_2d(projected)
    if circle is None:
        return None
    center_x, center_y, radius = circle
    if radius <= EPSILON:
        return None
    radii = [sqrt((point[0] - center_x) ** 2 + (point[1] - center_y) ** 2) for point in projected]
    if max(abs(item - radius) for item in radii) > max(1.0e-6, radius * 1.0e-5):
        return None
    center = _add(provisional_center, _add(_scale(first_axis, center_x), _scale(second_axis, center_y)))
    ordered_angles = []
    for point in projected:
        ordered_angles.append(atan2(point[1] - center_y, point[0] - center_x))
    winding = 1.0
    angle_steps = []
    for first, second in zip(ordered_angles, (*ordered_angles[1:], ordered_angles[0])):
        delta = second - first
        if delta <= -3.141592653589793:
            delta += 6.283185307179586
        elif delta > 3.141592653589793:
            delta -= 6.283185307179586
        angle_steps.append(delta)
    if sum(angle_steps) < 0.0:
        winding = -1.0

    return center, first_axis, second_axis, radius, winding


def _fit_circle_2d(points: Sequence[tuple[float, float]]) -> tuple[float, float, float] | None:
    ata = [[0.0, 0.0, 0.0] for _ in range(3)]
    atb = [0.0, 0.0, 0.0]
    for x, y in points:
        row = (x, y, 1.0)
        value = -(x * x + y * y)
        for i in range(3):
            atb[i] += row[i] * value
            for j in range(3):
                ata[i][j] += row[i] * row[j]
    solution = _solve_3x3(ata, atb)
    if solution is None:
        return None
    a, b, c = solution
    center_x = -0.5 * a
    center_y = -0.5 * b
    radius_squared = center_x * center_x + center_y * center_y - c
    if radius_squared <= EPSILON:
        return None
    return center_x, center_y, sqrt(radius_squared)


def _solve_3x3(matrix: list[list[float]], vector: list[float]) -> tuple[float, float, float] | None:
    rows = [matrix[index][:] + [vector[index]] for index in range(3)]
    for pivot_index in range(3):
        pivot = max(range(pivot_index, 3), key=lambda row: abs(rows[row][pivot_index]))
        if abs(rows[pivot][pivot_index]) <= EPSILON:
            return None
        rows[pivot_index], rows[pivot] = rows[pivot], rows[pivot_index]
        scale = rows[pivot_index][pivot_index]
        rows[pivot_index] = [value / scale for value in rows[pivot_index]]
        for row_index in range(3):
            if row_index == pivot_index:
                continue
            factor = rows[row_index][pivot_index]
            rows[row_index] = [
                value - factor * rows[pivot_index][column]
                for column, value in enumerate(rows[row_index])
            ]
    return rows[0][3], rows[1][3], rows[2][3]


def _centroid(points: Sequence[Vector3]) -> Vector3:
    scale = 1.0 / len(points)
    return (
        sum(point[0] for point in points) * scale,
        sum(point[1] for point in points) * scale,
        sum(point[2] for point in points) * scale,
    )


def _lerp(first: Vector3, second: Vector3, alpha: float) -> Vector3:
    return (
        first[0] + (second[0] - first[0]) * alpha,
        first[1] + (second[1] - first[1]) * alpha,
        first[2] + (second[2] - first[2]) * alpha,
    )


def _add(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] + second[0], first[1] + second[1], first[2] + second[2])


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _distance(first: Vector3, second: Vector3) -> float:
    return sqrt((first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2 + (first[2] - second[2]) ** 2)


def _length(vector: Vector3) -> float:
    return sqrt(_dot(vector, vector))


def _cross(first: Vector3, second: Vector3) -> Vector3:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _polygon_normal(vertices: Sequence[Vector3], face: Sequence[int]) -> Vector3:
    return _polygon_normal_from_points(tuple(vertices[index] for index in face))


def _polygon_normal_from_points(points: Sequence[Vector3]) -> Vector3:
    normal = (0.0, 0.0, 0.0)
    for first, second in zip(points, (*points[1:], points[0])):
        normal = (
            normal[0] + (first[1] - second[1]) * (first[2] + second[2]),
            normal[1] + (first[2] - second[2]) * (first[0] + second[0]),
            normal[2] + (first[0] - second[0]) * (first[1] + second[1]),
        )
    return normal


def _dot(first: Vector3, second: Vector3) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled()
