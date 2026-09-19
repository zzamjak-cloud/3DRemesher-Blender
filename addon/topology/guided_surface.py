from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isclose, sqrt
from statistics import median
from typing import Iterable, Sequence

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3


EPSILON = 1.0e-9
MAX_GUIDED_SURFACE_QUADS = 200000
MIN_FACE_LOOP_COUNT = 3
MIN_DIAGONAL_RATIO = 1.12
MAX_TARGET_ERROR_RATIO = 0.05


@dataclass(frozen=True)
class _QuadCandidate:
    score: float
    first_face: int
    second_face: int
    face: tuple[int, int, int, int]


@dataclass(frozen=True)
class _MatchedCycle:
    vertices: tuple[int, ...]
    score: float


def try_remesh_guided_surface(engine_input: EngineInput, *, cancelled: CancelledCallback | None = None) -> MeshData | None:
    """삼각화된 닫힌 표면에서 LOOP 가이드가 있는 쿼드 격자를 복원한다.

    현재 경로는 이미 존재하는 올쿼드 메시를 성공으로 처리하지 않는다. 닫힌
    합성 얼굴처럼 규칙 쿼드 격자를 대각선으로 삼각화한 입력에서 긴 공유
    대각선을 찾아 삼각형 두 개를 하나의 쿼드로 되돌리고, 각 LOOP 가이드가
    복원된 메시의 독립 폐루프 엣지 사이클에 명확히 대응할 때만 MeshData를
    반환한다. 애매한 pairing이나 가이드 매칭은 None으로 넘긴다.
    """
    _check_cancelled(cancelled)
    if not _supports_controls(engine_input):
        return None

    recovered = _recover_quad_grid(engine_input.mesh)
    if recovered is None or len(recovered.faces) > MAX_GUIDED_SURFACE_QUADS:
        return None
    if not _within_target_count(len(recovered.faces), engine_input.settings.target_quad_count):
        return None

    guide_loops = _loop_guide_splines(engine_input)
    if guide_loops is None or len(guide_loops) < MIN_FACE_LOOP_COUNT:
        return None

    edge_faces = _edge_faces(recovered.faces)
    edges = tuple(edge_faces)
    edge_length = _median_edge_length(recovered.vertices, edges)
    if edge_length <= EPSILON:
        return None

    used_vertices: set[int] = set()
    matched_cycles: list[_MatchedCycle] = []
    for guide_loop in guide_loops:
        _check_cancelled(cancelled)
        matched = _match_guide_cycle(recovered.vertices, edges, guide_loop, edge_length)
        if matched is None:
            return None
        if used_vertices.intersection(matched.vertices):
            return None
        used_vertices.update(matched.vertices)
        matched_cycles.append(matched)

    if not _cycles_have_quad_bands(tuple(cycle.vertices for cycle in matched_cycles), edge_faces):
        return None
    return recovered


def _within_target_count(actual_quad_count: int, target_quad_count: int) -> bool:
    if target_quad_count <= 0:
        return False
    error_ratio = abs(actual_quad_count - target_quad_count) / target_quad_count
    return error_ratio <= MAX_TARGET_ERROR_RATIO


def _supports_controls(engine_input: EngineInput) -> bool:
    settings = engine_input.settings
    if settings.symmetry_axes:
        return False
    if engine_input.mesh.hard_edges:
        return False
    if not isclose(settings.density_scale, 1.0, rel_tol=0.0, abs_tol=EPSILON):
        return False
    if not _density_values_are_uniform(engine_input.density_values):
        return False
    return True


def _density_values_are_uniform(values: Sequence[float]) -> bool:
    if not values:
        return True
    first = values[0]
    return all(isclose(value, first, rel_tol=0.0, abs_tol=EPSILON) for value in values[1:])


def _recover_quad_grid(mesh: MeshData) -> MeshData | None:
    if len(mesh.vertices) < 8 or not mesh.faces or any(len(face) != 3 for face in mesh.faces):
        return None
    if len(mesh.faces) % 2 != 0:
        return None

    source_edge_faces = _edge_faces(mesh.faces)
    if any(len(face_indices) != 2 for face_indices in source_edge_faces.values()):
        return None
    if not _is_connected(mesh.faces, len(mesh.vertices)):
        return None

    ordered = _recover_ordered_triangle_pairs(mesh)
    if ordered is not None:
        return ordered

    candidates: list[_QuadCandidate] = []
    for edge, (first_face, second_face) in source_edge_faces.items():
        candidate = _quad_candidate(mesh.vertices, mesh.faces, edge, first_face, second_face)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    used_faces: set[int] = set()
    recovered_faces: list[tuple[int, int, int, int]] = []
    for candidate in candidates:
        if candidate.first_face in used_faces or candidate.second_face in used_faces:
            continue
        used_faces.add(candidate.first_face)
        used_faces.add(candidate.second_face)
        recovered_faces.append(candidate.face)

    if len(used_faces) != len(mesh.faces):
        return None

    recovered = MeshData(vertices=mesh.vertices, faces=tuple(recovered_faces))
    edge_faces = _edge_faces(recovered.faces)
    if any(len(face_indices) != 2 for face_indices in edge_faces.values()):
        return None
    if not _is_connected(recovered.faces, len(recovered.vertices)):
        return None
    return recovered


def _recover_ordered_triangle_pairs(mesh: MeshData) -> MeshData | None:
    recovered_faces: list[tuple[int, int, int, int]] = []
    for face_index in range(0, len(mesh.faces), 2):
        first = tuple(mesh.faces[face_index])
        second = tuple(mesh.faces[face_index + 1])
        shared = tuple(sorted(set(first).intersection(second)))
        if len(shared) != 2:
            return None
        face = _quad_from_triangle_pair(first, second, (shared[0], shared[1]))
        if face is None:
            return None
        recovered_faces.append(face)

    recovered = MeshData(vertices=mesh.vertices, faces=tuple(recovered_faces))
    edge_faces = _edge_faces(recovered.faces)
    if any(len(face_indices) != 2 for face_indices in edge_faces.values()):
        return None
    if not _is_connected(recovered.faces, len(recovered.vertices)):
        return None
    return recovered


def _quad_candidate(
    vertices: Sequence[Vector3],
    faces: Sequence[Sequence[int]],
    shared_edge: tuple[int, int],
    first_face: int,
    second_face: int,
) -> _QuadCandidate | None:
    first = tuple(faces[first_face])
    second = tuple(faces[second_face])
    combined = tuple(dict.fromkeys(first + second))
    if len(combined) != 4:
        return None
    shared = set(shared_edge)
    first_other = next((vertex for vertex in first if vertex not in shared), None)
    second_other = next((vertex for vertex in second if vertex not in shared), None)
    if first_other is None or second_other is None:
        return None

    boundary_edges = _quad_boundary_edges(first_other, second_other, shared_edge)
    boundary_lengths = [_distance(vertices[a], vertices[b]) for a, b in boundary_edges]
    shared_length = _distance(vertices[shared_edge[0]], vertices[shared_edge[1]])
    typical_boundary = median(boundary_lengths)
    if typical_boundary <= EPSILON:
        return None
    ratio = shared_length / typical_boundary
    if ratio < MIN_DIAGONAL_RATIO:
        return None

    face = _directed_quad_from_triangles(first, second, shared_edge)
    if face is None:
        return None
    return _QuadCandidate(score=ratio, first_face=first_face, second_face=second_face, face=face)


def _quad_from_triangle_pair(
    first: Sequence[int],
    second: Sequence[int],
    shared_edge: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    shared = set(shared_edge)
    if sum(1 for vertex in first if vertex in shared) != 2:
        return None
    if sum(1 for vertex in second if vertex in shared) != 2:
        return None
    return _directed_quad_from_triangles(first, second, shared_edge)


def _quad_boundary_edges(
    first_other: int,
    second_other: int,
    shared_edge: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]:
    return (
        _edge_key(first_other, shared_edge[0]),
        _edge_key(first_other, shared_edge[1]),
        _edge_key(second_other, shared_edge[0]),
        _edge_key(second_other, shared_edge[1]),
    )


def _directed_quad_from_triangles(
    first: Sequence[int],
    second: Sequence[int],
    shared_edge: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    shared = _edge_key(shared_edge[0], shared_edge[1])
    boundary_edges = []
    for face in (first, second):
        for index, start in enumerate(face):
            end = face[(index + 1) % len(face)]
            if _edge_key(start, end) != shared:
                boundary_edges.append((start, end))
    if len(boundary_edges) != 4:
        return None

    outgoing: dict[int, int] = {}
    incoming: dict[int, int] = {}
    for start, end in boundary_edges:
        if start in outgoing or end in incoming:
            return None
        outgoing[start] = end
        incoming[end] = start
    if set(outgoing) != set(incoming):
        return None

    start = boundary_edges[0][0]
    ordered = [start]
    current = start
    while len(ordered) < 4:
        next_vertex = outgoing.get(current)
        if next_vertex is None:
            return None
        if next_vertex in ordered:
            return None
        ordered.append(next_vertex)
        current = next_vertex
    if outgoing.get(current) != start:
        return None
    return tuple(ordered)


def _loop_guide_splines(engine_input: EngineInput) -> tuple[tuple[Vector3, ...], ...] | None:
    loops: list[tuple[Vector3, ...]] = []
    for guide in engine_input.guide_curves:
        for spline, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            if kind != "LOOP" or not closed:
                return None
            points = _trim_closed_points(spline)
            if len(points) < 4:
                return None
            loops.append(points)
    return tuple(loops)


def _trim_closed_points(points: Sequence[Vector3]) -> tuple[Vector3, ...]:
    trimmed = tuple(points)
    if len(trimmed) >= 2 and _distance(trimmed[0], trimmed[-1]) <= EPSILON:
        trimmed = trimmed[:-1]
    return trimmed


def _match_guide_cycle(
    vertices: Sequence[Vector3],
    edges: Sequence[tuple[int, int]],
    guide_loop: Sequence[Vector3],
    edge_length: float,
) -> _MatchedCycle | None:
    guide_distances = tuple(_distance_to_polyline(vertex, guide_loop) for vertex in vertices)
    nearest_sample_distance = max(_nearest_vertex_distance(point, vertices) for point in guide_loop)
    tolerance = max(edge_length * 0.45, nearest_sample_distance + edge_length * 0.2)
    candidate_vertices = {index for index, distance in enumerate(guide_distances) if distance <= tolerance}
    if len(candidate_vertices) < 4:
        return None

    adjacency: dict[int, set[int]] = defaultdict(set)
    for first, second in edges:
        if first not in candidate_vertices or second not in candidate_vertices:
            continue
        midpoint = _scale(_add(vertices[first], vertices[second]), 0.5)
        if _distance_to_polyline(midpoint, guide_loop) > tolerance:
            continue
        adjacency[first].add(second)
        adjacency[second].add(first)

    candidates: list[_MatchedCycle] = []
    for component in _connected_components(adjacency):
        if len(component) < 4:
            continue
        if any(len(adjacency[vertex].intersection(component)) != 2 for vertex in component):
            continue
        ordered = _order_cycle(component, adjacency)
        if ordered is None:
            continue
        candidates.append(_MatchedCycle(vertices=ordered, score=_cycle_score(vertices, ordered, guide_loop)))

    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate.score)
    best = candidates[0]
    if len(candidates) > 1:
        ambiguity_gap = max(edge_length * 0.2, best.score * 0.35)
        if candidates[1].score - best.score <= ambiguity_gap:
            return None
    return best


def _cycles_have_quad_bands(
    cycles: Sequence[Sequence[int]],
    edge_faces: dict[tuple[int, int], tuple[int, ...]],
) -> bool:
    for cycle in cycles:
        face_indices: set[int] = set()
        for index, first in enumerate(cycle):
            second = cycle[(index + 1) % len(cycle)]
            faces = edge_faces.get(_edge_key(first, second))
            if faces is None or len(faces) != 2:
                return False
            face_indices.update(faces)
        # 루프 엣지 양쪽에 실제 쿼드 띠가 붙어 있어야 한다.
        if len(face_indices) < len(cycle):
            return False
    return True


def _edge_faces(faces: Sequence[Sequence[int]]) -> dict[tuple[int, int], tuple[int, ...]]:
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for index, first in enumerate(face):
            second = face[(index + 1) % len(face)]
            edge_faces[_edge_key(first, second)].append(face_index)
    return {edge: tuple(face_indices) for edge, face_indices in edge_faces.items()}


def _is_connected(faces: Sequence[Sequence[int]], vertex_count: int) -> bool:
    neighbors: list[set[int]] = [set() for _ in range(vertex_count)]
    used_vertices: set[int] = set()
    for face in faces:
        for index, first in enumerate(face):
            second = face[(index + 1) % len(face)]
            neighbors[first].add(second)
            neighbors[second].add(first)
            used_vertices.add(first)
            used_vertices.add(second)
    if len(used_vertices) != vertex_count:
        return False
    start = next(iter(used_vertices))
    visited = {start}
    queue = deque((start,))
    while queue:
        current = queue.popleft()
        for neighbor in neighbors[current]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited == used_vertices


def _connected_components(adjacency: dict[int, set[int]]) -> tuple[frozenset[int], ...]:
    remaining = set(adjacency)
    components: list[frozenset[int]] = []
    while remaining:
        start = remaining.pop()
        component = {start}
        queue = deque((start,))
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if neighbor in component:
                    continue
                component.add(neighbor)
                remaining.discard(neighbor)
                queue.append(neighbor)
        components.append(frozenset(component))
    return tuple(components)


def _order_cycle(component: frozenset[int], adjacency: dict[int, set[int]]) -> tuple[int, ...] | None:
    start = min(component)
    ordered = [start]
    previous = None
    current = start
    while True:
        neighbors = sorted(adjacency[current].intersection(component))
        if len(neighbors) != 2:
            return None
        next_vertex = neighbors[0] if neighbors[0] != previous else neighbors[1]
        if next_vertex == start:
            return tuple(ordered) if len(ordered) == len(component) else None
        if next_vertex in ordered:
            return None
        ordered.append(next_vertex)
        previous, current = current, next_vertex


def _cycle_score(vertices: Sequence[Vector3], cycle: Sequence[int], guide_loop: Sequence[Vector3]) -> float:
    vertex_error = sum(_distance_to_polyline(vertices[index], guide_loop) for index in cycle) / len(cycle)
    guide_error = sum(_nearest_cycle_vertex_distance(point, vertices, cycle) for point in guide_loop) / len(guide_loop)
    center_error = _distance(_center([vertices[index] for index in cycle]), _center(guide_loop))
    return vertex_error + guide_error + center_error * 0.25


def _median_edge_length(vertices: Sequence[Vector3], edges: Iterable[tuple[int, int]]) -> float:
    lengths = [_distance(vertices[first], vertices[second]) for first, second in edges]
    if not lengths:
        return 0.0
    return float(median(lengths))


def _distance_to_polyline(point: Vector3, polyline: Sequence[Vector3]) -> float:
    return min(
        _distance_to_segment(point, first, polyline[(index + 1) % len(polyline)])
        for index, first in enumerate(polyline)
    )


def _distance_to_segment(point: Vector3, first: Vector3, second: Vector3) -> float:
    segment = _sub(second, first)
    length_squared = _dot(segment, segment)
    if length_squared <= EPSILON:
        return _distance(point, first)
    t = max(0.0, min(1.0, _dot(_sub(point, first), segment) / length_squared))
    closest = _add(first, _scale(segment, t))
    return _distance(point, closest)


def _nearest_vertex_distance(point: Vector3, vertices: Sequence[Vector3]) -> float:
    return min(_distance(point, vertex) for vertex in vertices)


def _nearest_cycle_vertex_distance(point: Vector3, vertices: Sequence[Vector3], cycle: Sequence[int]) -> float:
    return min(_distance(point, vertices[index]) for index in cycle)


def _center(points: Sequence[Vector3]) -> Vector3:
    scale = 1.0 / len(points)
    return (
        sum(point[0] for point in points) * scale,
        sum(point[1] for point in points) * scale,
        sum(point[2] for point in points) * scale,
    )


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _add(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] + second[0], first[1] + second[1], first[2] + second[2])


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _scale(vector: Vector3, value: float) -> Vector3:
    return (vector[0] * value, vector[1] * value, vector[2] * value)


def _dot(first: Vector3, second: Vector3) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _distance(first: Vector3, second: Vector3) -> float:
    return sqrt(
        (first[0] - second[0]) * (first[0] - second[0])
        + (first[1] - second[1]) * (first[1] - second[1])
        + (first[2] - second[2]) * (first[2] - second[2])
    )


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("가이드 표면 리토폴로지가 취소되었습니다.")
