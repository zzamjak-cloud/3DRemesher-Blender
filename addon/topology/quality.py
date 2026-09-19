from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from math import sqrt
from typing import Callable, Mapping, Sequence

from ..core import MeshData, RemeshCancelled, Vector3
from ..surface import SurfaceIndex


EPSILON = 1.0e-9


@dataclass(frozen=True)
class LayoutIssue:
    code: str
    message: str
    subject: str = ""


@dataclass(frozen=True)
class RegularChartExpectation:
    name: str
    face_indices: tuple[int, ...]
    expected_quad_count: int | None = None
    max_unintended_poles: int = 0
    max_edge_spacing_cv: float | None = None
    max_face_aspect_ratio: float | None = None


@dataclass(frozen=True)
class EdgePathExpectation:
    name: str
    points: tuple[Vector3, ...]
    closed: bool
    tolerance: float | None = None
    endpoints_on_boundary: bool = False


@dataclass(frozen=True)
class LayoutExpectations:
    regular_charts: tuple[RegularChartExpectation, ...] = field(default_factory=tuple)
    loops: tuple[EdgePathExpectation, ...] = field(default_factory=tuple)
    strips: tuple[EdgePathExpectation, ...] = field(default_factory=tuple)
    boundary_edge_count: int | None = None
    max_non_manifold_edges: int = 0
    max_bowtie_vertices: int = 0
    max_face_aspect_ratio: float | None = None
    max_edge_spacing_cv: float | None = None


@dataclass(frozen=True)
class ChartQuality:
    name: str
    quad_count: int
    unintended_pole_count: int
    edge_spacing_cv: float
    max_face_aspect_ratio: float


@dataclass(frozen=True)
class LayoutMetrics:
    boundary_edge_count: int
    non_manifold_edge_count: int
    bowtie_vertex_count: int
    edge_spacing_cv: float
    max_face_aspect_ratio: float
    mean_face_aspect_ratio: float
    chart_metrics: tuple[ChartQuality, ...]


@dataclass(frozen=True)
class LayoutReport:
    metrics: LayoutMetrics
    issues: tuple[LayoutIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class BidirectionalSampleDistance:
    source_sample_count: int
    output_sample_count: int
    max_source_to_output: float
    mean_source_to_output: float
    max_output_to_source: float
    mean_output_to_source: float

    @property
    def max_distance(self) -> float:
        return max(self.max_source_to_output, self.max_output_to_source)


CancelledCallback = Callable[[], bool]


def validate_layout(mesh: MeshData, expectations: LayoutExpectations | Mapping[str, object] | None = None) -> LayoutReport:
    """선언된 차트와 가이드 대응을 엔진 경로와 독립적으로 검증한다."""

    expected = _coerce_expectations(expectations)
    mesh.validate()
    edge_faces = _edge_faces(mesh.faces)
    boundary_edges = frozenset(edge for edge, faces in edge_faces.items() if len(faces) == 1)
    non_manifold_edges = tuple(edge for edge, faces in edge_faces.items() if len(faces) > 2)
    bowtie_vertices = _bowtie_vertices(mesh)
    face_aspects = tuple(_face_aspect(mesh.vertices, face) for face in mesh.faces)
    edge_cv = _edge_spacing_cv(mesh.vertices, tuple(edge_faces))

    issues: list[LayoutIssue] = []
    if expected.boundary_edge_count is not None and len(boundary_edges) != expected.boundary_edge_count:
        issues.append(
            LayoutIssue(
                "BOUNDARY_EDGE_COUNT",
                f"경계 엣지 수가 예상과 다릅니다: expected={expected.boundary_edge_count}, actual={len(boundary_edges)}",
            )
        )
    if len(non_manifold_edges) > expected.max_non_manifold_edges:
        issues.append(
            LayoutIssue(
                "NON_MANIFOLD_EDGE",
                f"비다양체 엣지가 허용치를 넘었습니다: allowed={expected.max_non_manifold_edges}, actual={len(non_manifold_edges)}",
            )
        )
    if len(bowtie_vertices) > expected.max_bowtie_vertices:
        issues.append(
            LayoutIssue(
                "BOWTIE_VERTEX",
                f"보타이 정점이 허용치를 넘었습니다: allowed={expected.max_bowtie_vertices}, actual={len(bowtie_vertices)}",
            )
        )
    maximum_aspect = max(face_aspects, default=0.0)
    mean_aspect = sum(face_aspects) / len(face_aspects) if face_aspects else 0.0
    if expected.max_face_aspect_ratio is not None and maximum_aspect > expected.max_face_aspect_ratio:
        issues.append(
            LayoutIssue(
                "FACE_ASPECT",
                f"면 종횡비가 허용치를 넘었습니다: allowed={expected.max_face_aspect_ratio:.6g}, actual={maximum_aspect:.6g}",
            )
        )
    if expected.max_edge_spacing_cv is not None and edge_cv > expected.max_edge_spacing_cv:
        issues.append(
            LayoutIssue(
                "EDGE_SPACING_CV",
                f"엣지 간격 CV가 허용치를 넘었습니다: allowed={expected.max_edge_spacing_cv:.6g}, actual={edge_cv:.6g}",
            )
        )

    chart_metrics: list[ChartQuality] = []
    for chart in expected.regular_charts:
        quality, chart_issues = _validate_regular_chart(mesh, chart)
        chart_metrics.append(quality)
        issues.extend(chart_issues)

    graph_edges = frozenset(edge_faces)
    for loop in expected.loops:
        issues.extend(_validate_path(mesh, loop, graph_edges, boundary_edges, "LOOP"))
    for strip in expected.strips:
        issues.extend(_validate_path(mesh, strip, graph_edges, boundary_edges, "STRIP"))

    return LayoutReport(
        LayoutMetrics(
            boundary_edge_count=len(boundary_edges),
            non_manifold_edge_count=len(non_manifold_edges),
            bowtie_vertex_count=len(bowtie_vertices),
            edge_spacing_cv=edge_cv,
            max_face_aspect_ratio=maximum_aspect,
            mean_face_aspect_ratio=mean_aspect,
            chart_metrics=tuple(chart_metrics),
        ),
        tuple(issues),
    )


def measure_bidirectional_sample_distance(
    source: MeshData,
    output: MeshData,
    *,
    cancelled: CancelledCallback | None = None,
    chunk_size: int = 256,
    max_samples_per_mesh: int = 20000,
) -> BidirectionalSampleDistance:
    """정점과 면 중심 표본에서 상대 삼각 표면까지의 양방향 거리를 잰다."""

    if chunk_size < 1:
        raise ValueError("chunk_size는 1 이상이어야 합니다.")
    if max_samples_per_mesh < 1:
        raise ValueError("max_samples_per_mesh는 1 이상이어야 합니다.")
    source.validate()
    output.validate()
    source_samples = _limited_samples(_sample_points(source), max_samples_per_mesh)
    output_samples = _limited_samples(_sample_points(output), max_samples_per_mesh)
    _check_cancelled(cancelled)
    source_surface = SurfaceIndex(_triangulated_surface(source))
    _check_cancelled(cancelled)
    output_surface = SurfaceIndex(_triangulated_surface(output))
    source_to_output = _surface_distance(source_samples, output_surface, cancelled=cancelled, chunk_size=chunk_size)
    output_to_source = _surface_distance(output_samples, source_surface, cancelled=cancelled, chunk_size=chunk_size)
    return BidirectionalSampleDistance(
        source_sample_count=len(source_samples),
        output_sample_count=len(output_samples),
        max_source_to_output=source_to_output[0],
        mean_source_to_output=source_to_output[1],
        max_output_to_source=output_to_source[0],
        mean_output_to_source=output_to_source[1],
    )


def _coerce_expectations(expectations: LayoutExpectations | Mapping[str, object] | None) -> LayoutExpectations:
    if expectations is None:
        return LayoutExpectations()
    if isinstance(expectations, LayoutExpectations):
        return expectations
    return LayoutExpectations(**expectations)


def _validate_regular_chart(mesh: MeshData, chart: RegularChartExpectation) -> tuple[ChartQuality, tuple[LayoutIssue, ...]]:
    issues: list[LayoutIssue] = []
    invalid = tuple(index for index in chart.face_indices if index < 0 or index >= len(mesh.faces))
    if invalid:
        issues.append(LayoutIssue("CHART_FACE_INDEX", f"{chart.name} 차트가 잘못된 면 인덱스를 참조합니다.", chart.name))
    face_indices = tuple(index for index in chart.face_indices if 0 <= index < len(mesh.faces))
    faces = tuple(mesh.faces[index] for index in face_indices)
    quad_count = sum(1 for face in faces if len(face) == 4)
    if chart.expected_quad_count is not None and quad_count != chart.expected_quad_count:
        issues.append(
            LayoutIssue(
                "CHART_QUAD_COUNT",
                f"{chart.name} 차트 쿼드 수가 예상과 다릅니다: expected={chart.expected_quad_count}, actual={quad_count}",
                chart.name,
            )
        )
    non_quads = len(faces) - quad_count
    if non_quads:
        issues.append(LayoutIssue("CHART_NGON", f"{chart.name} 차트에 쿼드가 아닌 면이 있습니다: actual={non_quads}", chart.name))

    edge_counts = _edge_faces(faces)
    boundary_vertices = {vertex for edge, linked in edge_counts.items() if len(linked) == 1 for vertex in edge}
    local_degree: dict[int, int] = defaultdict(int)
    for first, second in edge_counts:
        local_degree[first] += 1
        local_degree[second] += 1

    unintended: list[int] = []
    for vertex, degree in sorted(local_degree.items()):
        if vertex in boundary_vertices:
            if degree not in {2, 3}:
                unintended.append(vertex)
        elif degree != 4:
            unintended.append(vertex)
    if len(unintended) > chart.max_unintended_poles:
        issues.append(
            LayoutIssue(
                "CHART_UNINTENDED_POLE",
                f"{chart.name} 차트의 의도치 않은 pole이 허용치를 넘었습니다: allowed={chart.max_unintended_poles}, actual={len(unintended)}",
                chart.name,
            )
        )

    chart_aspects = tuple(_face_aspect(mesh.vertices, face) for face in faces)
    max_aspect = max(chart_aspects, default=0.0)
    if chart.max_face_aspect_ratio is not None and max_aspect > chart.max_face_aspect_ratio:
        issues.append(
            LayoutIssue(
                "CHART_FACE_ASPECT",
                f"{chart.name} 차트 면 종횡비가 허용치를 넘었습니다: allowed={chart.max_face_aspect_ratio:.6g}, actual={max_aspect:.6g}",
                chart.name,
            )
        )
    edge_cv = _edge_spacing_cv(mesh.vertices, tuple(edge_counts))
    if chart.max_edge_spacing_cv is not None and edge_cv > chart.max_edge_spacing_cv:
        issues.append(
            LayoutIssue(
                "CHART_EDGE_SPACING_CV",
                f"{chart.name} 차트 엣지 간격 CV가 허용치를 넘었습니다: allowed={chart.max_edge_spacing_cv:.6g}, actual={edge_cv:.6g}",
                chart.name,
            )
        )
    return ChartQuality(chart.name, quad_count, len(unintended), edge_cv, max_aspect), tuple(issues)


def _validate_path(
    mesh: MeshData,
    path: EdgePathExpectation,
    graph_edges: frozenset[tuple[int, int]],
    boundary_edges: frozenset[tuple[int, int]],
    label: str,
) -> tuple[LayoutIssue, ...]:
    issues: list[LayoutIssue] = []
    tolerance = path.tolerance if path.tolerance is not None else max(_mean_edge_length(mesh, graph_edges) * 0.35, 1.0e-6)
    guide_points = path.points
    if path.closed and len(guide_points) >= 2 and _distance(guide_points[0], guide_points[-1]) <= EPSILON:
        guide_points = guide_points[:-1]
    matched = _nearest_vertices(mesh.vertices, guide_points, tolerance)
    if matched is None:
        return (LayoutIssue(f"{label}_VERTEX_MISSING", f"{path.name} 경로가 허용 거리 안의 출력 정점을 찾지 못했습니다.", path.name),)

    sampled = [(vertex, point) for vertex, point in zip(matched, guide_points)]
    compact = []
    for item in sampled:
        if not compact or compact[-1][0] != item[0]:
            compact.append(item)
    if path.closed and len(compact) >= 2 and compact[0][0] == compact[-1][0]:
        compact = compact[:-1]
    vertices = tuple(vertex for vertex, _point in compact)
    sampled_points = tuple(point for _vertex, point in compact)
    minimum = 3 if path.closed else 2
    if len(vertices) < minimum:
        issues.append(LayoutIssue(f"{label}_VERTEX_MISSING", f"{path.name} 경로에 대응된 고유 정점이 부족합니다.", path.name))
        return tuple(issues)
    if len(set(vertices)) != len(vertices):
        issues.append(LayoutIssue(f"{label}_DUPLICATE_VERTEX", f"{path.name} 경로가 같은 출력 정점을 반복해서 사용합니다.", path.name))

    graph_neighbors = _edge_neighbors(graph_edges)
    corridor = max(tolerance, _mean_edge_length(mesh, graph_edges) * 0.8)
    edge_pairs = list(zip(vertices, vertices[1:]))
    guide_pairs = list(zip(sampled_points, sampled_points[1:]))
    if path.closed:
        edge_pairs.append((vertices[-1], vertices[0]))
        guide_pairs.append((sampled_points[-1], sampled_points[0]))
    used_interior: set[int] = set()
    missing: list[tuple[int, int]] = []
    for (first, second), (guide_start, guide_end) in zip(edge_pairs, guide_pairs):
        forbidden = used_interior | (set(vertices) - {first, second})
        chain = _edge_chain(
            graph_neighbors, mesh.vertices, first, second,
            guide_start, guide_end, corridor, forbidden,
        )
        if chain is None:
            missing.append(_edge_key(first, second))
        else:
            used_interior.update(chain[1:-1])
    if missing:
        issues.append(LayoutIssue(f"{label}_EDGE_MISSING", f"{path.name} 경로의 연속 엣지가 끊어졌습니다: missing={len(missing)}", path.name))

    if path.endpoints_on_boundary and not path.closed:
        start_edges = tuple(edge for edge in boundary_edges if vertices[0] in edge)
        end_edges = tuple(edge for edge in boundary_edges if vertices[-1] in edge)
        if not start_edges or not end_edges:
            issues.append(LayoutIssue(f"{label}_ENDPOINT_NOT_BOUNDARY", f"{path.name} 경로 끝점이 열린 경계에 놓이지 않았습니다.", path.name))
    return tuple(issues)


def _edge_faces(faces: Sequence[Sequence[int]]) -> dict[tuple[int, int], tuple[int, ...]]:
    pending: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for first, second in zip(face, (*face[1:], face[0])):
            pending[_edge_key(first, second)].append(face_index)
    return {edge: tuple(indices) for edge, indices in pending.items()}


def _edge_neighbors(edges: Sequence[tuple[int, int]]) -> dict[int, frozenset[int]]:
    pending: dict[int, set[int]] = defaultdict(set)
    for first, second in edges:
        pending[first].add(second)
        pending[second].add(first)
    return {vertex: frozenset(neighbors) for vertex, neighbors in pending.items()}


def _edge_chain(
    neighbors: Mapping[int, frozenset[int]],
    vertices: Sequence[Vector3],
    start: int,
    end: int,
    guide_start: Vector3,
    guide_end: Vector3,
    tolerance: float,
    forbidden: set[int],
) -> tuple[int, ...] | None:
    if start == end:
        return None
    remaining = deque((start,))
    seen = {start}
    previous: dict[int, int] = {}
    while remaining:
        current = remaining.popleft()
        for neighbor in sorted(neighbors.get(current, ())):
            if neighbor in forbidden or neighbor in seen:
                continue
            midpoint = tuple((vertices[current][axis] + vertices[neighbor][axis]) * 0.5 for axis in range(3))
            if _distance_point_to_segment(vertices[neighbor], guide_start, guide_end) > tolerance:
                continue
            if _distance_point_to_segment(midpoint, guide_start, guide_end) > tolerance:
                continue
            previous[neighbor] = current
            if neighbor == end:
                chain = [end]
                while chain[-1] != start:
                    chain.append(previous[chain[-1]])
                return tuple(reversed(chain))
            seen.add(neighbor)
            remaining.append(neighbor)
    return None


def _distance_point_to_segment(point: Vector3, start: Vector3, end: Vector3) -> float:
    offset = tuple(end[axis] - start[axis] for axis in range(3))
    length_squared = sum(component * component for component in offset)
    if length_squared <= EPSILON * EPSILON:
        return _distance(point, start)
    ratio = sum((point[axis] - start[axis]) * offset[axis] for axis in range(3)) / length_squared
    ratio = min(1.0, max(0.0, ratio))
    nearest = tuple(start[axis] + ratio * offset[axis] for axis in range(3))
    return _distance(point, nearest)


def _bowtie_vertices(mesh: MeshData) -> tuple[int, ...]:
    incident: dict[int, list[int]] = defaultdict(list)
    for face_index, face in enumerate(mesh.faces):
        for vertex in set(face):
            incident[vertex].append(face_index)

    face_edges = tuple(frozenset(_edge_key(first, second) for first, second in zip(face, (*face[1:], face[0]))) for face in mesh.faces)
    bowties: list[int] = []
    for vertex, face_indices in incident.items():
        if len(face_indices) <= 1:
            continue
        neighbors: dict[int, set[int]] = {face_index: set() for face_index in face_indices}
        for index, first in enumerate(face_indices):
            for second in face_indices[index + 1 :]:
                if any(vertex in edge for edge in face_edges[first] & face_edges[second]):
                    neighbors[first].add(second)
                    neighbors[second].add(first)
        if _component_count(neighbors) > 1:
            bowties.append(vertex)
    return tuple(bowties)


def _component_count(neighbors: Mapping[int, set[int]]) -> int:
    remaining = set(neighbors)
    count = 0
    while remaining:
        count += 1
        start = remaining.pop()
        queue: deque[int] = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in neighbors[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
    return count


def _face_aspect(vertices: Sequence[Vector3], face: Sequence[int]) -> float:
    lengths = [_distance(vertices[first], vertices[second]) for first, second in zip(face, (*face[1:], face[0]))]
    shortest = min(lengths, default=0.0)
    longest = max(lengths, default=0.0)
    if shortest <= EPSILON:
        return float("inf")
    return longest / shortest


def _edge_spacing_cv(vertices: Sequence[Vector3], edges: Sequence[tuple[int, int]]) -> float:
    lengths = tuple(_distance(vertices[first], vertices[second]) for first, second in edges)
    if not lengths:
        return 0.0
    mean = sum(lengths) / len(lengths)
    if mean <= EPSILON:
        return 0.0
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    return sqrt(variance) / mean


def _mean_edge_length(mesh: MeshData, edges: Sequence[tuple[int, int]]) -> float:
    if not edges:
        return 0.0
    return sum(_distance(mesh.vertices[first], mesh.vertices[second]) for first, second in edges) / len(edges)


def _nearest_vertices(vertices: Sequence[Vector3], points: Sequence[Vector3], tolerance: float) -> tuple[int, ...] | None:
    matched: list[int] = []
    limit = tolerance * tolerance
    for point in points:
        best_index = -1
        best_distance = float("inf")
        for index, vertex in enumerate(vertices):
            distance = _distance_squared(point, vertex)
            if distance < best_distance:
                best_index = index
                best_distance = distance
        if best_index < 0 or best_distance > limit:
            return None
        matched.append(best_index)
    return tuple(matched)


def _remove_consecutive_duplicates(vertices: Sequence[int]) -> tuple[int, ...]:
    result: list[int] = []
    for vertex in vertices:
        if not result or result[-1] != vertex:
            result.append(vertex)
    return tuple(result)


def _sample_points(mesh: MeshData) -> tuple[Vector3, ...]:
    centers = tuple(_centroid(tuple(mesh.vertices[index] for index in face)) for face in mesh.faces)
    return (*mesh.vertices, *centers)


def _limited_samples(points: Sequence[Vector3], limit: int) -> tuple[Vector3, ...]:
    if len(points) <= limit:
        return tuple(points)
    if limit == 1:
        return (points[0],)
    last = len(points) - 1
    return tuple(points[round(index * last / (limit - 1))] for index in range(limit))


def _surface_distance(
    points: Sequence[Vector3],
    surface: SurfaceIndex,
    *,
    cancelled: CancelledCallback | None,
    chunk_size: int,
) -> tuple[float, float]:
    if not points:
        return 0.0, 0.0
    total = 0.0
    maximum = 0.0
    for start in range(0, len(points), chunk_size):
        _check_cancelled(cancelled)
        for point in points[start : start + chunk_size]:
            distance = surface.nearest(point)[1]
            maximum = max(maximum, distance)
            total += distance
    _check_cancelled(cancelled)
    return maximum, total / len(points)


def _triangulated_surface(mesh: MeshData) -> MeshData:
    from ..engine import _triangulated

    return _triangulated(mesh, mesh.hard_edges)


def _centroid(points: Sequence[Vector3]) -> Vector3:
    count = len(points)
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _distance(first: Vector3, second: Vector3) -> float:
    return sqrt(_distance_squared(first, second))


def _distance_squared(first: Vector3, second: Vector3) -> float:
    return sum((first[index] - second[index]) ** 2 for index in range(3))


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled()
