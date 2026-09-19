from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import cos, isfinite, pi, sqrt
from typing import Callable, Sequence

from .core import MeshData, RemeshCancelled, Vector3, analyze_mesh
from .surface import SurfaceIndex


ProgressCallback = Callable[[float, str], None]
CancelledCallback = Callable[[], bool]
Quadric = tuple[float, float, float, float, float, float, float, float, float, float, float]

EPSILON = 1.0e-9
MAX_SOURCE_TRIANGLES = 20000
MAX_OUTPUT_TRIANGLES = 200000
FEATURE_STRAIGHT_COSINE = cos(160.0 * pi / 180.0)
MAX_COLLAPSE_RMS_FACTOR = 0.04


@dataclass(frozen=True)
class _Topology:
    edge_faces: dict[tuple[int, int], tuple[int, ...]]
    vertex_faces: dict[int, frozenset[int]]
    vertex_neighbors: dict[int, frozenset[int]]
    face_keys: frozenset[tuple[int, int, int]]


@dataclass(frozen=True)
class _FeatureState:
    fixed_vertices: frozenset[int]
    graph: dict[int, frozenset[int]]


@dataclass(frozen=True)
class _CollapsePlan:
    removed_count: int
    new_point: Vector3
    cost: float
    keep: int
    remove: int
    combined_quadric: Quadric


def adapt_triangles(
    mesh: MeshData,
    target_triangles: int,
    density_values: tuple[float, ...] = (),
    *,
    density_scale: float = 1.0,
    cancelled: CancelledCallback | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[MeshData, tuple[float, ...], tuple[str, ...]]:
    if target_triangles < 1:
        raise ValueError("목표 삼각형 수는 1 이상이어야 합니다.")
    if density_scale <= 0.0 or not isfinite(density_scale):
        raise ValueError("밀도 배율은 유한한 양수여야 합니다.")
    mesh.validate()
    _validate_density(mesh, density_values)
    _validate_tri_mesh(mesh)
    if len(mesh.faces) > MAX_SOURCE_TRIANGLES:
        raise ValueError(f"소스 삼각형 수가 너무 많습니다: {len(mesh.faces)} > {MAX_SOURCE_TRIANGLES}")

    warnings: list[str] = []
    vertices = [tuple(vertex) for vertex in mesh.vertices]
    densities = list(density_values) if density_values else [1.0 for _ in vertices]
    faces: list[tuple[int, int, int] | None] = [tuple(face) for face in mesh.faces]
    hard_edges = {_edge_key(*edge) for edge in mesh.hard_edges}
    quadrics = _build_vertex_quadrics(vertices, faces)
    max_collapse_rms = _bbox_diagonal(vertices) * MAX_COLLAPSE_RMS_FACTOR
    feature_state = _build_feature_state(vertices, hard_edges)
    surface = SurfaceIndex(mesh)
    surface_limit = max(max(v[a] for v in vertices)-min(v[a] for v in vertices) for a in range(3)) * .025

    _check_cancelled(cancelled)
    _report(progress, 0.05, "적응형 삼각 메시 검증")

    if target_triangles < _active_face_count(faces):
        _reduce_mesh(
            vertices,
            densities,
            quadrics,
            faces,
            hard_edges,
            feature_state,
            target_triangles,
            density_scale,
            max_collapse_rms,
            cancelled,
            progress,
            surface, surface_limit,
        )
    elif target_triangles > _active_face_count(faces):
        _increase_mesh(vertices, densities, quadrics, faces, hard_edges, target_triangles, density_scale, cancelled, progress)

    if density_values and _has_density_contrast(densities) and _active_face_count(faces) == target_triangles:
        _redistribute_density(vertices, densities, quadrics, faces, hard_edges, density_scale, max_collapse_rms, cancelled, progress, surface, surface_limit)

    _compact(vertices, densities, faces, hard_edges)
    result_mesh = MeshData(vertices=tuple(vertices), faces=tuple(face for face in faces if face is not None), hard_edges=frozenset(hard_edges))
    result_density = tuple(densities)
    _validate_tri_mesh(result_mesh)
    analysis = analyze_mesh(result_mesh)
    if analysis.non_manifold_edge_count:
        raise ValueError("적응형 결과가 비다양체 엣지를 포함합니다.")
    if analysis.degenerate_face_count:
        raise ValueError("적응형 결과가 퇴화 면을 포함합니다.")
    if analysis.face_count != target_triangles:
        warnings.append(f"목표 삼각형 수와 실제 삼각형 수가 다릅니다: target={target_triangles}, actual={analysis.face_count}")
    _report(progress, 1.0, "적응형 삼각 메시 완료")
    return result_mesh, result_density, tuple(warnings)


def _reduce_mesh(
    vertices: list[Vector3],
    densities: list[float],
    quadrics: list[Quadric],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    feature_state: _FeatureState,
    target_triangles: int,
    density_scale: float,
    max_collapse_rms: float,
    cancelled: CancelledCallback | None,
    progress: ProgressCallback | None,
    surface: SurfaceIndex,
    surface_limit: float,
) -> None:
    blocked_edges = set()
    start_count = _active_face_count(faces)
    while _active_face_count(faces) > target_triangles:
        _check_cancelled(cancelled)
        current_count = _active_face_count(faces)
        _report(progress, 0.1 + 0.55 * (start_count - current_count) / max(1, start_count - target_triangles), "삼각형 감소")
        topology = _build_topology(faces)
        protected_vertices = _protected_vertices_from_topology(topology, hard_edges, feature_state)
        candidates: list[tuple[float, tuple[int, int], _CollapsePlan]] = []
        for candidate_index, edge in enumerate(topology.edge_faces):
            if edge in blocked_edges:
                continue
            if candidate_index % 128 == 0:
                _check_cancelled(cancelled)
            plan = _collapse_plan(
                vertices,
                densities,
                quadrics,
                faces,
                hard_edges,
                protected_vertices,
                feature_state,
                topology,
                edge,
                density_scale,
                max_collapse_rms,
            )
            if plan is not None and current_count - plan.removed_count >= target_triangles:
                candidates.append((plan.cost, edge, plan))
        candidates.sort(key=lambda item: (item[0], item[1]))
        candidates = _low_cost_candidate_batch(candidates)

        accepted = False
        blocked = False
        used_vertices: set[int] = set()
        for candidate_index, (_, edge, _) in enumerate(candidates):
            if candidate_index % 128 == 0:
                _check_cancelled(cancelled)
            if edge[0] in used_vertices or edge[1] in used_vertices:
                continue
            topology = _build_topology(faces)
            feature_state = _build_feature_state(vertices, hard_edges)
            protected_vertices = _protected_vertices_from_topology(topology, hard_edges, feature_state)
            plan = _collapse_plan(
                vertices,
                densities,
                quadrics,
                faces,
                hard_edges,
                protected_vertices,
                feature_state,
                topology,
                edge,
                density_scale,
                max_collapse_rms,
            )
            if plan is None:
                continue
            current_count = _active_face_count(faces)
            if current_count - plan.removed_count < target_triangles:
                continue
            if not _surface_safe_collapse(vertices, faces, topology, plan, surface, surface_limit):
                blocked_edges.add(edge)
                blocked = True
                continue
            _apply_collapse(vertices, densities, quadrics, faces, hard_edges, plan)
            used_vertices.update(edge)
            accepted = True
            feature_state = _build_feature_state(vertices, hard_edges)
            _check_cancelled(cancelled)
        if not accepted and not blocked:
            break


def _low_cost_candidate_batch(
    candidates: Sequence[tuple[float, tuple[int, int], _CollapsePlan]],
) -> list[tuple[float, tuple[int, int], _CollapsePlan]]:
    if len(candidates) <= 5:
        return list(candidates)
    limit = max(1, len(candidates) // 5)
    return list(candidates[:limit])


def _increase_mesh(
    vertices: list[Vector3],
    densities: list[float],
    quadrics: list[Quadric],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    target_triangles: int,
    density_scale: float,
    cancelled: CancelledCallback | None,
    progress: ProgressCallback | None,
) -> None:
    start_count = _active_face_count(faces)
    while _active_face_count(faces) < target_triangles:
        _check_cancelled(cancelled)
        current_count = _active_face_count(faces)
        if current_count >= MAX_OUTPUT_TRIANGLES:
            break
        _report(progress, 0.1 + 0.8 * (current_count - start_count) / max(1, target_triangles - start_count), "삼각형 증가")
        edge_faces = _edge_faces(faces)
        candidates: list[tuple[float, tuple[int, int], tuple[int, ...]]] = []
        for candidate_index, (edge, incident_faces) in enumerate(edge_faces.items()):
            if candidate_index % 256 == 0:
                _check_cancelled(cancelled)
            added_count = len(incident_faces)
            if current_count + added_count > min(target_triangles, MAX_OUTPUT_TRIANGLES):
                continue
            density = _edge_density(densities, edge, density_scale)
            score = _distance_squared(vertices[edge[0]], vertices[edge[1]]) * density * density
            candidates.append((score, edge, incident_faces))
        if not candidates:
            break
        candidates.sort(key=lambda item: (-item[0], item[1]))
        used_faces: set[int] = set()
        split_count = 0
        for candidate_index, (_, edge, incident_faces) in enumerate(candidates):
            if candidate_index % 256 == 0:
                _check_cancelled(cancelled)
            added_count = len(incident_faces)
            current_count = _active_face_count(faces)
            if current_count + added_count > min(target_triangles, MAX_OUTPUT_TRIANGLES):
                continue
            if any(face_index in used_faces for face_index in incident_faces):
                continue
            _split_edge(vertices, densities, quadrics, faces, hard_edges, edge, incident_faces)
            used_faces.update(incident_faces)
            split_count += 1
        if split_count == 0:
            break


def _redistribute_density(
    vertices: list[Vector3],
    densities: list[float],
    quadrics: list[Quadric],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    density_scale: float,
    max_collapse_rms: float,
    cancelled: CancelledCallback | None,
    progress: ProgressCallback | None,
    surface: SurfaceIndex,
    surface_limit: float,
) -> None:
    budget = max(1, min(32, _active_face_count(faces) // 4))
    protected_vertices = _protected_vertices(faces, hard_edges)
    for iteration in range(budget):
        _check_cancelled(cancelled)
        _report(progress, 0.15 + 0.65 * iteration / budget, "밀도 재분포")
        topology = _build_topology(faces)
        feature_state = _build_feature_state(vertices, hard_edges)
        edge_faces = topology.edge_faces
        protected_vertices = _protected_vertices_from_topology(topology, hard_edges, feature_state)
        collapse_options: list[tuple[float, tuple[int, int], _CollapsePlan]] = []
        for edge in edge_faces:
            plan = _collapse_plan(
                vertices,
                densities,
                quadrics,
                faces,
                hard_edges,
                protected_vertices,
                feature_state,
                topology,
                edge,
                density_scale,
                max_collapse_rms,
            )
            if plan is not None and plan.removed_count == 2:
                collapse_options.append((_edge_density(densities, edge, density_scale), edge, plan))
        if not collapse_options:
            return
        collapse_options.sort(key=lambda item: (item[0], item[1]))
        selected = next((item for item in collapse_options if _surface_safe_collapse(vertices, faces, topology, item[2], surface, surface_limit)),None)
        if selected is None:
            return
        _, collapse_edge, collapse_plan = selected
        collapse_faces = set(edge_faces[collapse_edge])

        split_options: list[tuple[float, tuple[int, int], tuple[int, ...]]] = []
        for edge, incident_faces in edge_faces.items():
            if set(incident_faces) & collapse_faces:
                continue
            if len(incident_faces) != collapse_plan.removed_count:
                continue
            score = _distance_squared(vertices[edge[0]], vertices[edge[1]]) * _edge_density(densities, edge, density_scale)
            split_options.append((score, edge, incident_faces))
        if not split_options:
            return
        split_options.sort(key=lambda item: (-item[0], item[1]))
        _, split_edge, split_faces = split_options[0]
        if _edge_density(densities, split_edge, density_scale) <= _edge_density(densities, collapse_edge, density_scale):
            return

        _split_edge(vertices, densities, quadrics, faces, hard_edges, split_edge, split_faces)
        _apply_collapse(vertices, densities, quadrics, faces, hard_edges, collapse_plan)


def _surface_safe_collapse(vertices, faces, topology, plan, surface, limit):
    if surface.nearest(plan.new_point)[1] > limit:
        return False
    incident=topology.vertex_faces[plan.keep] | topology.vertex_faces[plan.remove]
    samples=[]
    for i in incident:
        face=faces[i]
        if plan.keep in face and plan.remove in face:
            continue
        points=[plan.new_point if v in (plan.keep,plan.remove) else vertices[v] for v in face]
        samples.append(tuple(sum(p[a] for p in points)/3 for a in range(3)))
        samples.extend(_midpoint(points[j],points[(j+1)%3]) for j in range(3))
    return all(surface.nearest(p)[1] <= limit for p in samples)


def _collapse_plan(
    vertices: Sequence[Vector3],
    densities: Sequence[float],
    quadrics: Sequence[Quadric],
    faces: Sequence[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    protected_vertices: set[int],
    feature_state: _FeatureState,
    topology: _Topology,
    edge: tuple[int, int],
    density_scale: float,
    max_collapse_rms: float,
) -> _CollapsePlan | None:
    first, second = edge
    combined_quadric = _add_quadrics(quadrics[first], quadrics[second])
    target = _collapse_target(vertices, combined_quadric, edge, hard_edges, protected_vertices, feature_state)
    if target is None:
        return None
    keep, remove, new_point = target
    if _quadric_rms(combined_quadric, new_point) > max_collapse_rms:
        return None
    incident_faces = topology.edge_faces.get(edge, ())
    if not incident_faces:
        return None
    if not _passes_link_condition(edge, topology):
        return None
    removed_count = len(incident_faces)
    if removed_count <= 0:
        return None
    if not _collapse_keeps_valid_faces(vertices, faces, keep, remove, new_point, topology):
        return None
    plane_error = _quadric_error(combined_quadric, new_point)
    density = _edge_density(densities, edge, density_scale)
    cost = (_distance_squared(vertices[first], vertices[second]) * 0.01 + plane_error) * density * density
    return _CollapsePlan(
        removed_count=removed_count,
        new_point=new_point,
        cost=cost,
        keep=keep,
        remove=remove,
        combined_quadric=combined_quadric,
    )


def _collapse_target(
    vertices: Sequence[Vector3],
    combined_quadric: Quadric,
    edge: tuple[int, int],
    hard_edges: set[tuple[int, int]],
    protected_vertices: set[int],
    feature_state: _FeatureState,
) -> tuple[int, int, Vector3] | None:
    first, second = edge
    first_fixed = first in protected_vertices
    second_fixed = second in protected_vertices
    feature_vertices = set(feature_state.graph)

    if edge in hard_edges:
        if first_fixed and second_fixed:
            return None
        if first_fixed:
            return first, second, vertices[first]
        if second_fixed:
            return second, first, vertices[second]
        keep, remove = (first, second) if first < second else (second, first)
        return keep, remove, _best_edge_point(vertices[first], vertices[second], combined_quadric)

    if first in feature_vertices or second in feature_vertices:
        return None
    if first_fixed or second_fixed:
        return None
    keep, remove = (first, second) if first < second else (second, first)
    return keep, remove, _best_edge_point(vertices[first], vertices[second], combined_quadric)


def _apply_collapse(
    vertices: list[Vector3],
    densities: list[float],
    quadrics: list[Quadric],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    plan: _CollapsePlan,
) -> None:
    keep = plan.keep
    remove = plan.remove
    vertices[keep] = plan.new_point
    densities[keep] = (densities[keep] + densities[remove]) * 0.5
    quadrics[keep] = plan.combined_quadric
    vertices[remove] = plan.new_point
    quadrics[remove] = _zero_quadric()

    for index, face in enumerate(faces):
        if face is None:
            continue
        updated = tuple(keep if vertex == remove else vertex for vertex in face)
        if len(set(updated)) < 3:
            faces[index] = None
        else:
            faces[index] = updated

    updated_hard_edges: set[tuple[int, int]] = set()
    for first, second in hard_edges:
        first = keep if first == remove else first
        second = keep if second == remove else second
        if first != second:
            updated_hard_edges.add(_edge_key(first, second))
    hard_edges.clear()
    hard_edges.update(updated_hard_edges)


def _split_edge(
    vertices: list[Vector3],
    densities: list[float],
    quadrics: list[Quadric],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
    edge: tuple[int, int],
    incident_faces: Sequence[int],
) -> None:
    first, second = edge
    vertices.append(_midpoint(vertices[first], vertices[second]))
    densities.append((densities[first] + densities[second]) * 0.5)
    quadrics.append(_scale_quadric(_add_quadrics(quadrics[first], quadrics[second]), 0.5))
    midpoint_index = len(vertices) - 1

    for face_index in tuple(incident_faces):
        face = faces[face_index]
        if face is None:
            continue
        other = next(vertex for vertex in face if vertex not in edge)
        if _contains_directed_edge(face, first, second):
            faces[face_index] = (first, midpoint_index, other)
            faces.append((midpoint_index, second, other))
        else:
            faces[face_index] = (second, midpoint_index, other)
            faces.append((midpoint_index, first, other))

    if edge in hard_edges:
        hard_edges.remove(edge)
        hard_edges.add(_edge_key(first, midpoint_index))
        hard_edges.add(_edge_key(midpoint_index, second))


def _validate_density(mesh: MeshData, density_values: tuple[float, ...]) -> None:
    if density_values and len(density_values) != len(mesh.vertices):
        raise ValueError("밀도 값 개수는 메시 정점 개수와 같아야 합니다.")
    for index, value in enumerate(density_values):
        if not isfinite(value) or value < 0.0:
            raise ValueError(f"{index}번 밀도 값은 0 이상의 유한한 숫자여야 합니다.")


def _validate_tri_mesh(mesh: MeshData) -> None:
    analysis = analyze_mesh(mesh)
    if analysis.triangle_count != analysis.face_count:
        raise ValueError("적응형 재표본화 입력은 삼각형 메시여야 합니다.")
    if analysis.non_manifold_edge_count:
        raise ValueError("적응형 재표본화 입력은 비다양체 엣지를 포함할 수 없습니다.")
    if analysis.degenerate_face_count:
        raise ValueError("적응형 재표본화 입력은 퇴화 면을 포함할 수 없습니다.")

    seen_faces: set[tuple[int, int, int]] = set()
    directed_edges: dict[tuple[int, int], int] = {}
    for face_index, face in enumerate(mesh.faces):
        key = tuple(sorted(face))
        if key in seen_faces:
            raise ValueError(f"{face_index}번 면은 중복 면입니다.")
        seen_faces.add(key)
        for first, second in _face_edges(face):
            edge = _edge_key(first, second)
            if edge in directed_edges and directed_edges[edge] == first:
                raise ValueError(f"{edge} 엣지를 공유하는 면들의 winding이 일관되지 않습니다.")
            directed_edges[edge] = first


def _collapse_keeps_valid_faces(
    vertices: Sequence[Vector3],
    faces: Sequence[tuple[int, int, int] | None],
    keep: int,
    remove: int,
    new_point: Vector3,
    topology: _Topology,
) -> bool:
    seen: set[tuple[int, int, int]] = set()
    affected_faces = topology.vertex_faces.get(keep, frozenset()) | topology.vertex_faces.get(remove, frozenset())
    for face_index in affected_faces:
        face = faces[face_index]
        if face is None:
            continue
        updated = tuple(keep if vertex == remove else vertex for vertex in face)
        if len(set(updated)) < 3:
            continue
        positions = [new_point if vertex == keep else vertices[vertex] for vertex in updated]
        normal = _triangle_normal(*positions)
        if _length(normal) <= EPSILON:
            return False
        if keep in updated:
            old_positions = [vertices[vertex] for vertex in face]
            old_normal = _triangle_normal(*old_positions)
            if _dot(old_normal, normal) <= EPSILON:
                return False
        key = tuple(sorted(updated))
        if key in seen:
            return False
        old_key = tuple(sorted(face))
        if key != old_key and key in topology.face_keys:
            return False
        seen.add(key)
    return True


def _passes_link_condition(
    edge: tuple[int, int],
    topology: _Topology,
) -> bool:
    first, second = edge
    first_neighbors = topology.vertex_neighbors.get(first, frozenset())
    second_neighbors = topology.vertex_neighbors.get(second, frozenset())
    common_neighbors = (first_neighbors & second_neighbors) - {first, second}
    return len(common_neighbors) == len(topology.edge_faces.get(edge, ()))


def _protected_vertices(
    faces: Sequence[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
) -> set[int]:
    edge_faces = _edge_faces(faces)
    protected: set[int] = set()
    for edge, incident_faces in edge_faces.items():
        if len(incident_faces) == 1 or edge in hard_edges:
            protected.update(edge)
    return protected


def _protected_vertices_from_topology(
    topology: _Topology,
    hard_edges: set[tuple[int, int]],
    feature_state: _FeatureState,
) -> set[int]:
    protected: set[int] = set(feature_state.fixed_vertices)
    for edge, incident_faces in topology.edge_faces.items():
        if len(incident_faces) == 1 and edge not in hard_edges:
            protected.update(edge)
    return protected


def _plane_error(
    vertices: Sequence[Vector3],
    faces: Sequence[tuple[int, int, int] | None],
    edge: tuple[int, int],
    point: Vector3,
    topology: _Topology,
) -> float:
    total = 0.0
    affected_faces = topology.vertex_faces.get(edge[0], frozenset()) | topology.vertex_faces.get(edge[1], frozenset())
    for face_index in affected_faces:
        face = faces[face_index]
        if face is None or edge[0] not in face and edge[1] not in face:
            continue
        a, b, c = (vertices[index] for index in face)
        normal = _normalize(_triangle_normal(a, b, c))
        distance = _dot(normal, _sub(point, a))
        total += distance * distance
    return total


def _build_topology(faces: Sequence[tuple[int, int, int] | None]) -> _Topology:
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    vertex_faces: dict[int, set[int]] = defaultdict(set)
    vertex_neighbors: dict[int, set[int]] = defaultdict(set)
    face_keys: set[tuple[int, int, int]] = set()
    for face_index, face in enumerate(faces):
        if face is None:
            continue
        face_keys.add(tuple(sorted(face)))
        for vertex in face:
            vertex_faces[vertex].add(face_index)
            vertex_neighbors[vertex].update(item for item in face if item != vertex)
        for first, second in _face_edges(face):
            edge_faces[_edge_key(first, second)].append(face_index)
    return _Topology(
        edge_faces={edge: tuple(indices) for edge, indices in edge_faces.items()},
        vertex_faces={vertex: frozenset(indices) for vertex, indices in vertex_faces.items()},
        vertex_neighbors={vertex: frozenset(neighbors) for vertex, neighbors in vertex_neighbors.items()},
        face_keys=frozenset(face_keys),
    )


def _build_vertex_quadrics(vertices: Sequence[Vector3], faces: Sequence[tuple[int, int, int] | None]) -> list[Quadric]:
    quadrics = [_zero_quadric() for _ in vertices]
    for face in faces:
        if face is None:
            continue
        a, b, c = (vertices[index] for index in face)
        normal = _normalize(_triangle_normal(a, b, c))
        if _length(normal) <= EPSILON:
            continue
        plane = (normal[0], normal[1], normal[2], -_dot(normal, a))
        quadric = _plane_quadric(plane)
        for vertex_index in face:
            quadrics[vertex_index] = _add_quadrics(quadrics[vertex_index], quadric)
    return quadrics


def _best_edge_point(first: Vector3, second: Vector3, quadric: Quadric) -> Vector3:
    midpoint = _midpoint(first, second)
    candidates = [first, second, midpoint]
    edge_minimum = _edge_quadric_minimum(first, second, quadric)
    if edge_minimum is not None:
        candidates.append(edge_minimum)
    return min(candidates, key=lambda point: (_quadric_error(quadric, point), _distance_squared(point, midpoint)))


def _edge_quadric_minimum(first: Vector3, second: Vector3, quadric: Quadric) -> Vector3 | None:
    midpoint = _midpoint(first, second)
    f0 = _quadric_error(quadric, first)
    f1 = _quadric_error(quadric, second)
    fm = _quadric_error(quadric, midpoint)
    quadratic = 2.0 * f1 + 2.0 * f0 - 4.0 * fm
    if abs(quadratic) <= EPSILON:
        return None
    linear = f1 - f0 - quadratic
    ratio = -linear / (2.0 * quadratic)
    if ratio <= 0.0 or ratio >= 1.0:
        return None
    return (
        first[0] + (second[0] - first[0]) * ratio,
        first[1] + (second[1] - first[1]) * ratio,
        first[2] + (second[2] - first[2]) * ratio,
    )


def _plane_quadric(plane: tuple[float, float, float, float]) -> Quadric:
    a, b, c, d = plane
    return (
        a * a,
        a * b,
        a * c,
        a * d,
        b * b,
        b * c,
        b * d,
        c * c,
        c * d,
        d * d,
        1.0,
    )


def _zero_quadric() -> Quadric:
    return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _add_quadrics(first: Quadric, second: Quadric) -> Quadric:
    return tuple(first[index] + second[index] for index in range(11))  # type: ignore[return-value]


def _scale_quadric(quadric: Quadric, scale: float) -> Quadric:
    return tuple(value * scale for value in quadric)  # type: ignore[return-value]


def _quadric_error(quadric: Quadric, point: Vector3) -> float:
    x, y, z = point
    return max(
        0.0,
        quadric[0] * x * x
        + 2.0 * quadric[1] * x * y
        + 2.0 * quadric[2] * x * z
        + 2.0 * quadric[3] * x
        + quadric[4] * y * y
        + 2.0 * quadric[5] * y * z
        + 2.0 * quadric[6] * y
        + quadric[7] * z * z
        + 2.0 * quadric[8] * z
        + quadric[9],
    )


def _quadric_rms(quadric: Quadric, point: Vector3) -> float:
    weight = max(1.0, quadric[10])
    return sqrt(_quadric_error(quadric, point) / weight)


def _bbox_diagonal(vertices: Sequence[Vector3]) -> float:
    min_x = min(vertex[0] for vertex in vertices)
    min_y = min(vertex[1] for vertex in vertices)
    min_z = min(vertex[2] for vertex in vertices)
    max_x = max(vertex[0] for vertex in vertices)
    max_y = max(vertex[1] for vertex in vertices)
    max_z = max(vertex[2] for vertex in vertices)
    return max(EPSILON, _length((max_x - min_x, max_y - min_y, max_z - min_z)))


def _build_feature_state(vertices: Sequence[Vector3], hard_edges: set[tuple[int, int]]) -> _FeatureState:
    graph: dict[int, set[int]] = defaultdict(set)
    for first, second in hard_edges:
        graph[first].add(second)
        graph[second].add(first)

    fixed_vertices: set[int] = set()
    for vertex, neighbors in graph.items():
        if len(neighbors) != 2:
            fixed_vertices.add(vertex)
            continue
        first, second = tuple(neighbors)
        first_vector = _normalize(_sub(vertices[first], vertices[vertex]))
        second_vector = _normalize(_sub(vertices[second], vertices[vertex]))
        if _dot(first_vector, second_vector) > FEATURE_STRAIGHT_COSINE:
            fixed_vertices.add(vertex)

    return _FeatureState(
        fixed_vertices=frozenset(fixed_vertices),
        graph={vertex: frozenset(neighbors) for vertex, neighbors in graph.items()},
    )


def _compact(
    vertices: list[Vector3],
    densities: list[float],
    faces: list[tuple[int, int, int] | None],
    hard_edges: set[tuple[int, int]],
) -> None:
    used_vertices = sorted({vertex for face in faces if face is not None for vertex in face})
    mapping = {old: new for new, old in enumerate(used_vertices)}
    vertices[:] = [vertices[index] for index in used_vertices]
    densities[:] = [densities[index] for index in used_vertices]
    faces[:] = [tuple(mapping[vertex] for vertex in face) for face in faces if face is not None]
    hard_edges.intersection_update({_edge_key(first, second) for first, second in hard_edges if first in mapping and second in mapping})
    remapped_edges = {_edge_key(mapping[first], mapping[second]) for first, second in hard_edges if first in mapping and second in mapping}
    hard_edges.clear()
    hard_edges.update(remapped_edges)


def _edge_density(densities: Sequence[float], edge: tuple[int, int], density_scale: float) -> float:
    average = (densities[edge[0]] + densities[edge[1]]) * 0.5
    return max(0.05, average) ** density_scale


def _has_density_contrast(densities: Sequence[float]) -> bool:
    if not densities:
        return False
    return max(densities) > min(densities) * 1.5 + EPSILON


def _edge_faces(faces: Sequence[tuple[int, int, int] | None]) -> dict[tuple[int, int], tuple[int, ...]]:
    result: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        if face is None:
            continue
        for first, second in _face_edges(face):
            result[_edge_key(first, second)].append(face_index)
    return {edge: tuple(indices) for edge, indices in result.items()}


def _vertex_neighbors(faces: Sequence[tuple[int, int, int] | None], vertex: int) -> set[int]:
    neighbors: set[int] = set()
    for face in faces:
        if face is None or vertex not in face:
            continue
        neighbors.update(item for item in face if item != vertex)
    return neighbors


def _active_face_count(faces: Sequence[tuple[int, int, int] | None]) -> int:
    return sum(1 for face in faces if face is not None)


def _face_edges(face: Sequence[int]) -> tuple[tuple[int, int], ...]:
    return tuple(zip(face, (*face[1:], face[0])))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _contains_directed_edge(face: Sequence[int], first: int, second: int) -> bool:
    return any(current == first and following == second for current, following in _face_edges(face))


def _triangle_normal(a: Vector3, b: Vector3, c: Vector3) -> Vector3:
    ab = _sub(b, a)
    ac = _sub(c, a)
    return (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )


def _midpoint(first: Vector3, second: Vector3) -> Vector3:
    return ((first[0] + second[0]) * 0.5, (first[1] + second[1]) * 0.5, (first[2] + second[2]) * 0.5)


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _dot(first: Vector3, second: Vector3) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _length(vector: Vector3) -> float:
    return sqrt(_dot(vector, vector))


def _normalize(vector: Vector3) -> Vector3:
    length = _length(vector)
    if length <= EPSILON:
        return (0.0, 0.0, 0.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _distance_squared(first: Vector3, second: Vector3) -> float:
    delta = _sub(first, second)
    return _dot(delta, delta)


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(fraction, message)


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("적응형 삼각 재표본화가 취소되었습니다.")
