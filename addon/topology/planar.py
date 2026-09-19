"""평면 차트 기반의 제한적 격자 리토폴로지."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isclose, sqrt
from typing import Sequence

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3


PLANAR_EPSILON = 1.0e-6
NORMAL_EPSILON = 1.0e-8
MAX_CHARTS = 128
MAX_SEGMENTS_PER_EDGE = 256
MAX_OUTPUT_QUADS = 200000


@dataclass(frozen=True)
class _FaceInfo:
    normal: Vector3
    offset: float


@dataclass(frozen=True)
class _Chart:
    faces: frozenset[int]
    normal: Vector3
    corners: tuple[int, int, int, int]
    side_ids: tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]
    side_lengths: tuple[float, float, float, float]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(self, item: tuple[int, int]) -> tuple[int, int]:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, first: tuple[int, int], second: tuple[int, int]) -> None:
        left = self.find(first)
        right = self.find(second)
        if left != right:
            self.parent[max(left, right)] = min(left, right)


def try_remesh_planar(engine_input: EngineInput, *, cancelled: CancelledCallback | None = None) -> MeshData | None:
    """지원 가능한 닫힌 평면 차트 메시를 균일 격자 쿼드 메시로 변환한다.

    현재 경로는 가이드, 대칭, 밀도 제어를 임의로 무시하지 않는다. 해당 제어가
    들어오면 일반 엔진 경로가 처리하도록 ``None``을 반환한다.
    """

    _check_cancelled(cancelled)
    settings = engine_input.settings
    if engine_input.guide_curves or settings.guide_curve_names:
        return None
    if not _has_uniform_density(engine_input.density_values) or not isclose(settings.density_scale, 1.0):
        return None

    mesh = engine_input.mesh
    try:
        settings.validate()
        mesh.validate()
    except ValueError:
        return None
    if settings.symmetry_axes and not _mesh_is_symmetric(mesh, settings.symmetry_axes):
        return None

    topology = _build_topology(mesh)
    if topology is None or not _is_closed_connected(topology[0], len(mesh.faces)):
        return None
    edge_faces, face_infos = topology
    charts = _extract_charts(mesh, edge_faces, face_infos)
    if charts is None:
        return None
    if not _hard_edges_are_chart_boundaries(mesh, charts, edge_faces):
        return None

    counts = _choose_edge_counts(charts, settings.target_quad_count)
    if counts is None:
        return None
    return _build_grid_mesh(mesh, charts, counts, cancelled)


def _build_topology(mesh: MeshData) -> tuple[dict[tuple[int, int], tuple[int, ...]], tuple[_FaceInfo, ...]] | None:
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    face_infos: list[_FaceInfo] = []
    seen_faces: set[tuple[int, ...]] = set()
    for face_index, face in enumerate(mesh.faces):
        if len(set(face)) != len(face):
            return None
        face_key = tuple(sorted(face))
        if face_key in seen_faces:
            return None
        seen_faces.add(face_key)
        normal = _polygon_normal(mesh.vertices, face)
        length = _length(normal)
        if length <= NORMAL_EPSILON:
            return None
        unit_normal = _mul(normal, 1.0 / length)
        offset = _dot(unit_normal, mesh.vertices[face[0]])
        if any(abs(_dot(unit_normal, mesh.vertices[index]) - offset) > PLANAR_EPSILON for index in face):
            return None
        face_infos.append(_FaceInfo(unit_normal, offset))
        for first, second in _face_edges(face):
            edge_faces[_edge_key(first, second)].append(face_index)
    if any(len(faces) != 2 for faces in edge_faces.values()):
        return None
    return {edge: tuple(faces) for edge, faces in edge_faces.items()}, tuple(face_infos)


def _is_closed_connected(edge_faces: dict[tuple[int, int], tuple[int, ...]], face_count: int) -> bool:
    neighbors: dict[int, set[int]] = defaultdict(set)
    for faces in edge_faces.values():
        first, second = faces
        neighbors[first].add(second)
        neighbors[second].add(first)
    seen = {0}
    queue: deque[int] = deque([0])
    while queue:
        current = queue.popleft()
        for neighbor in neighbors[current]:
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return len(seen) == face_count


def _extract_charts(
    mesh: MeshData,
    edge_faces: dict[tuple[int, int], tuple[int, ...]],
    face_infos: Sequence[_FaceInfo],
) -> tuple[_Chart, ...] | None:
    face_neighbors: dict[int, set[int]] = defaultdict(set)
    for faces in edge_faces.values():
        first, second = faces
        if _same_plane(face_infos[first], face_infos[second]):
            face_neighbors[first].add(second)
            face_neighbors[second].add(first)

    remaining = set(range(len(mesh.faces)))
    charts: list[_Chart] = []
    while remaining:
        start = min(remaining)
        faces = {start}
        queue: deque[int] = deque([start])
        remaining.remove(start)
        while queue:
            current = queue.popleft()
            for neighbor in face_neighbors[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    faces.add(neighbor)
                    queue.append(neighbor)
        chart = _build_chart(mesh, faces, edge_faces, face_infos[start].normal)
        if chart is None:
            return None
        charts.append(chart)
        if len(charts) > MAX_CHARTS:
            return None
    return tuple(charts)


def _build_chart(
    mesh: MeshData,
    faces: set[int],
    edge_faces: dict[tuple[int, int], tuple[int, ...]],
    normal: Vector3,
) -> _Chart | None:
    boundary_edges: list[tuple[int, int]] = []
    for face_index in sorted(faces):
        for first, second in _face_edges(mesh.faces[face_index]):
            linked = edge_faces[_edge_key(first, second)]
            if any(neighbor not in faces for neighbor in linked):
                boundary_edges.append((first, second))
    loop = _ordered_loop(boundary_edges)
    if loop is None:
        return None
    if _signed_projected_area(mesh.vertices, loop, normal) < 0.0:
        loop = (loop[0], *reversed(loop[1:]))
    corners = _corner_loop(mesh.vertices, loop)
    if corners is None or len(corners) != 4:
        return None
    if not _is_convex_quad(mesh.vertices, corners, normal):
        return None
    side_lengths = tuple(_path_length(mesh.vertices, loop, corners[index], corners[(index + 1) % 4]) for index in range(4))
    if any(length <= PLANAR_EPSILON for length in side_lengths):
        return None
    side_ids = tuple(_edge_key(corners[index], corners[(index + 1) % 4]) for index in range(4))
    return _Chart(frozenset(faces), normal, tuple(corners), side_ids, side_lengths)  # type: ignore[arg-type]


def _ordered_loop(edges: Sequence[tuple[int, int]]) -> tuple[int, ...] | None:
    neighbors: dict[int, list[int]] = defaultdict(list)
    for first, second in edges:
        neighbors[first].append(second)
        neighbors[second].append(first)
    if any(len(linked) != 2 for linked in neighbors.values()):
        return None
    start = min(neighbors)
    previous = -1
    current = start
    loop: list[int] = []
    for _ in range(len(neighbors)):
        loop.append(current)
        candidates = sorted(vertex for vertex in neighbors[current] if vertex != previous)
        if not candidates:
            return None
        following = candidates[0]
        previous, current = current, following
        if current in loop:
            return tuple(loop) if current == start and len(loop) == len(neighbors) else None
    return None


def _corner_loop(vertices: Sequence[Vector3], loop: Sequence[int]) -> tuple[int, ...] | None:
    corners: list[int] = []
    count = len(loop)
    for index, vertex in enumerate(loop):
        previous = vertices[loop[index - 1]]
        current = vertices[vertex]
        following = vertices[loop[(index + 1) % count]]
        if _length(_cross(_sub(current, previous), _sub(following, current))) > PLANAR_EPSILON:
            corners.append(vertex)
    return tuple(corners) if corners else None


def _path_length(vertices: Sequence[Vector3], loop: Sequence[int], start: int, end: int) -> float:
    total = 0.0
    index = loop.index(start)
    while loop[index] != end:
        following = (index + 1) % len(loop)
        total += _distance(vertices[loop[index]], vertices[loop[following]])
        index = following
    return total


def _hard_edges_are_chart_boundaries(
    mesh: MeshData,
    charts: Sequence[_Chart],
    edge_faces: dict[tuple[int, int], tuple[int, ...]],
) -> bool:
    face_to_chart: dict[int, int] = {}
    for chart_index, chart in enumerate(charts):
        for face_index in chart.faces:
            face_to_chart[face_index] = chart_index
    for first, second in mesh.hard_edges:
        edge = _edge_key(first, second)
        linked = edge_faces.get(edge)
        if linked is None or face_to_chart[linked[0]] == face_to_chart[linked[1]]:
            return False
    return True


def _has_uniform_density(density_values: Sequence[float]) -> bool:
    if not density_values:
        return True
    first = density_values[0]
    return all(isclose(value, first, rel_tol=1.0e-9, abs_tol=1.0e-9) for value in density_values)


def _mesh_is_symmetric(mesh: MeshData, axes: Sequence[str]) -> bool:
    vertex_keys = {_point_key(vertex) for vertex in mesh.vertices}
    face_keys = {frozenset(_point_key(mesh.vertices[index]) for index in face) for face in mesh.faces}
    for axis in axes:
        axis_index = "XYZ".index(axis)
        for vertex in mesh.vertices:
            mirrored = tuple(-component if index == axis_index else component for index, component in enumerate(vertex))
            if _point_key(mirrored) not in vertex_keys:
                return False
        for face in mesh.faces:
            mirrored_face = frozenset(
                _point_key(
                    tuple(-component if index == axis_index else component for index, component in enumerate(mesh.vertices[vertex_index]))
                )
                for vertex_index in face
            )
            if mirrored_face not in face_keys:
                return False
    return True


def _choose_edge_counts(charts: Sequence[_Chart], target_quad_count: int) -> dict[tuple[int, int], int] | None:
    union = _UnionFind()
    side_lengths: dict[tuple[int, int], list[float]] = defaultdict(list)
    for chart in charts:
        union.union(chart.side_ids[0], chart.side_ids[2])
        union.union(chart.side_ids[1], chart.side_ids[3])
        for side_id, length in zip(chart.side_ids, chart.side_lengths):
            side_lengths[side_id].append(length)

    class_lengths: dict[tuple[int, int], list[float]] = defaultdict(list)
    for side_id, lengths in side_lengths.items():
        class_lengths[union.find(side_id)].extend(lengths)
    lengths = {key: sum(values) / len(values) for key, values in class_lengths.items()}
    if not lengths:
        return None

    max_segments = min(MAX_SEGMENTS_PER_EDGE, max(1, int(sqrt(MAX_OUTPUT_QUADS / max(1, len(charts))))))
    area_sum = sum(lengths[union.find(chart.side_ids[0])] * lengths[union.find(chart.side_ids[1])] for chart in charts)
    desired_scale = sqrt(max(1, target_quad_count) / area_sum) if area_sum > 0.0 else 1.0
    scales = {desired_scale}
    for length in lengths.values():
        for segments in range(1, max_segments + 1):
            scales.add((segments - 0.5) / length)
            scales.add(segments / length)
            scales.add((segments + 0.5) / length)

    best: tuple[tuple[float, float, int], dict[tuple[int, int], int]] | None = None
    for scale in scales:
        class_counts = {
            key: max(1, min(max_segments, int(length * scale + 0.5)))
            for key, length in lengths.items()
        }
        total = sum(class_counts[union.find(chart.side_ids[0])] * class_counts[union.find(chart.side_ids[1])] for chart in charts)
        if total > MAX_OUTPUT_QUADS:
            continue
        relative_size_error = max(abs(class_counts[key] / lengths[key] - scale) for key in class_counts)
        rank = (abs(total - target_quad_count), relative_size_error, total)
        if best is None or rank < best[0]:
            best = (rank, class_counts)
    if best is None:
        return None
    return {side_id: best[1][union.find(side_id)] for side_id in side_lengths}


def _build_grid_mesh(
    source: MeshData,
    charts: Sequence[_Chart],
    counts: dict[tuple[int, int], int],
    cancelled: CancelledCallback | None,
) -> MeshData:
    vertices = [tuple(vertex) for vertex in source.vertices]
    edge_points: dict[tuple[int, int, int, int], int] = {}
    faces: list[tuple[int, int, int, int]] = []
    hard_edges: set[tuple[int, int]] = set()

    def edge_point(start: int, end: int, step: int, segments: int) -> int:
        if step == 0:
            return start
        if step == segments:
            return end
        low, high = _edge_key(start, end)
        key_step = step if (start, end) == (low, high) else segments - step
        key = (low, high, key_step, segments)
        if key not in edge_points:
            vertices.append(_lerp(vertices[low], vertices[high], key_step / segments))
            edge_points[key] = len(vertices) - 1
        return edge_points[key]

    for chart_index, chart in enumerate(charts):
        if chart_index % 16 == 0:
            _check_cancelled(cancelled)
        c0, c1, c2, c3 = chart.corners
        u_segments = counts[chart.side_ids[0]]
        v_segments = counts[chart.side_ids[1]]
        grid: list[list[int]] = []
        for y in range(v_segments + 1):
            row: list[int] = []
            for x in range(u_segments + 1):
                if y == 0:
                    row.append(edge_point(c0, c1, x, u_segments))
                elif y == v_segments:
                    row.append(edge_point(c3, c2, x, u_segments))
                elif x == 0:
                    row.append(edge_point(c0, c3, y, v_segments))
                elif x == u_segments:
                    row.append(edge_point(c1, c2, y, v_segments))
                else:
                    u = x / u_segments
                    v = y / v_segments
                    vertices.append(_bilinear(vertices[c0], vertices[c1], vertices[c2], vertices[c3], u, v))
                    row.append(len(vertices) - 1)
            grid.append(row)
        for y in range(v_segments):
            for x in range(u_segments):
                faces.append((grid[y][x], grid[y][x + 1], grid[y + 1][x + 1], grid[y + 1][x]))
        for start, end, segments in ((c0, c1, u_segments), (c1, c2, v_segments), (c2, c3, u_segments), (c3, c0, v_segments)):
            chain = [edge_point(start, end, step, segments) for step in range(segments + 1)]
            for first, second in zip(chain, chain[1:]):
                hard_edges.add(_edge_key(first, second))

    return _compact_mesh(vertices, faces, hard_edges)


def _compact_mesh(
    vertices: Sequence[Vector3],
    faces: Sequence[tuple[int, int, int, int]],
    hard_edges: set[tuple[int, int]],
) -> MeshData:
    remap: dict[int, int] = {}
    compact_vertices: list[Vector3] = []

    def mapped(index: int) -> int:
        if index not in remap:
            remap[index] = len(compact_vertices)
            compact_vertices.append(vertices[index])
        return remap[index]

    compact_faces = tuple(tuple(mapped(index) for index in face) for face in faces)
    compact_hard_edges = frozenset(
        _edge_key(remap[first], remap[second])
        for first, second in hard_edges
        if first in remap and second in remap
    )
    return MeshData(tuple(compact_vertices), compact_faces, compact_hard_edges)


def _same_plane(first: _FaceInfo, second: _FaceInfo) -> bool:
    if _dot(first.normal, second.normal) < 1.0 - PLANAR_EPSILON:
        return False
    return abs(first.offset - second.offset) <= PLANAR_EPSILON


def _is_convex_quad(vertices: Sequence[Vector3], corners: Sequence[int], normal: Vector3) -> bool:
    signs = []
    for index, current in enumerate(corners):
        previous = vertices[corners[index - 1]]
        point = vertices[current]
        following = vertices[corners[(index + 1) % 4]]
        signs.append(_dot(_cross(_sub(point, previous), _sub(following, point)), normal))
    return all(sign > PLANAR_EPSILON for sign in signs) or all(sign < -PLANAR_EPSILON for sign in signs)


def _signed_projected_area(vertices: Sequence[Vector3], loop: Sequence[int], normal: Vector3) -> float:
    axis = max(range(3), key=lambda index: abs(normal[index]))
    area = 0.0
    for first, second in zip(loop, (*loop[1:], loop[0])):
        a = vertices[first]
        b = vertices[second]
        if axis == 0:
            area += a[1] * b[2] - b[1] * a[2]
        elif axis == 1:
            area += a[2] * b[0] - b[2] * a[0]
        else:
            area += a[0] * b[1] - b[0] * a[1]
    return area * 0.5 * (1.0 if normal[axis] >= 0.0 else -1.0)


def _face_edges(face: Sequence[int]) -> tuple[tuple[int, int], ...]:
    return tuple(zip(face, (*face[1:], face[0])))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _polygon_normal(vertices: Sequence[Vector3], face: Sequence[int]) -> Vector3:
    normal = (0.0, 0.0, 0.0)
    for first, second in _face_edges(face):
        a = vertices[first]
        b = vertices[second]
        normal = (
            normal[0] + (a[1] - b[1]) * (a[2] + b[2]),
            normal[1] + (a[2] - b[2]) * (a[0] + b[0]),
            normal[2] + (a[0] - b[0]) * (a[1] + b[1]),
        )
    return normal


def _bilinear(a: Vector3, b: Vector3, c: Vector3, d: Vector3, u: float, v: float) -> Vector3:
    top = _lerp(a, b, u)
    bottom = _lerp(d, c, u)
    return _lerp(top, bottom, v)


def _lerp(a: Vector3, b: Vector3, t: float) -> Vector3:
    return tuple(a[index] + (b[index] - a[index]) * t for index in range(3))  # type: ignore[return-value]


def _sub(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: Vector3, scale: float) -> Vector3:
    return (a[0] * scale, a[1] * scale, a[2] * scale)


def _dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(vector: Vector3) -> float:
    return sqrt(_dot(vector, vector))


def _distance(a: Vector3, b: Vector3) -> float:
    return _length(_sub(a, b))


def _point_key(point: Sequence[float]) -> tuple[int, int, int]:
    return tuple(round(component / PLANAR_EPSILON) for component in point)  # type: ignore[return-value]


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled()
