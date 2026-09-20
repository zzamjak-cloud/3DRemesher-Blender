"""독립 표면 컴포넌트의 구조화 리메시 결과를 안전하게 결합한다."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from math import sqrt

from ..core import (
    CancelledCallback,
    EngineInput,
    GuideCurveData,
    MeshData,
    RemeshCancelled,
    Vector3,
    build_engine_input,
)
from ..surface import SurfaceIndex
from .face_patch import try_remesh_face_patch
from .branch_t import try_remesh_branch_t
from .rounded_branch import try_remesh_rounded_branch
from .guided_surface import try_remesh_guided_surface
from .periodic import try_remesh_periodic
from .planar import try_remesh_planar
from .triangulated_limb import try_remesh_triangulated_limb
from .irregular_tube import try_remesh_irregular_tube


_BUILDERS = (
    try_remesh_planar,
    try_remesh_periodic,
    try_remesh_triangulated_limb,
    try_remesh_irregular_tube,
    try_remesh_branch_t,
    try_remesh_rounded_branch,
    try_remesh_face_patch,
    try_remesh_guided_surface,
)
_MAX_COMPONENTS = 256


def try_remesh_components(
    engine_input: EngineInput, *, cancelled: CancelledCallback | None = None
) -> MeshData | None:
    """모든 독립 표면을 지원할 때만 분리해 리메시한 결과를 합친다.

    같은 좌표의 정점도 원본 인덱스가 다르면 별개로 유지한다. 전체 대칭은
    컴포넌트 간 대응을 요구하므로 현재 경로에서 지원하지 않는다.
    """
    _check_cancelled(cancelled)
    mesh = engine_input.mesh
    settings = engine_input.settings
    try:
        mesh.validate()
        settings.validate()
    except ValueError:
        return None
    if settings.symmetry_axes or (settings.guide_curve_names and not engine_input.guide_curves):
        return None
    components = _split_components(mesh, engine_input.density_values)
    if components is None or len(components) < 2 or len(components) > _MAX_COMPONENTS:
        return None
    if settings.target_quad_count < 4 * len(components):
        return None

    guide_groups = _assign_guides(engine_input.guide_curves, components, cancelled)
    if guide_groups is None:
        return None
    known_names = {guide.name for guide in engine_input.guide_curves}
    if any(name not in known_names for name in settings.guide_curve_names):
        return None
    budgets = _area_budgets(components, settings.target_quad_count)
    if budgets is None:
        return None

    all_vertices: list[Vector3] = []
    all_faces: list[tuple[int, ...]] = []
    all_hard_edges: set[tuple[int, int]] = set()
    for index, (source, density) in enumerate(components):
        _check_cancelled(cancelled)
        guides = tuple(guide_groups[index])
        names = tuple(name for name in settings.guide_curve_names if any(guide.name == name for guide in guides))
        local_settings = replace(settings, target_quad_count=budgets[index], guide_curve_names=names)
        local_input = build_engine_input(source, local_settings, guides, density)
        output = None
        for builder in _BUILDERS:
            _check_cancelled(cancelled)
            output = builder(local_input, cancelled=cancelled)
            if output is not None:
                break
        if output is None or not _preserves_component_topology(source, output):
            return None
        offset = len(all_vertices)
        all_vertices.extend(output.vertices)
        all_faces.extend(tuple(vertex + offset for vertex in face) for face in output.faces)
        all_hard_edges.update((first + offset, second + offset) for first, second in output.hard_edges)

    combined = MeshData(tuple(all_vertices), tuple(all_faces), frozenset(all_hard_edges))
    return combined if _component_count(combined) == len(components) else None


def _split_components(
    mesh: MeshData, density: tuple[float, ...]
) -> tuple[tuple[MeshData, tuple[float, ...]], ...] | None:
    if density and len(density) != len(mesh.vertices):
        return None
    vertex_faces: dict[int, list[int]] = defaultdict(list)
    for face_index, face in enumerate(mesh.faces):
        for vertex in face:
            vertex_faces[vertex].append(face_index)
    if len(vertex_faces) != len(mesh.vertices):
        return None
    pending = set(range(len(mesh.faces)))
    face_groups: list[tuple[int, ...]] = []
    while pending:
        start = min(pending)
        pending.remove(start)
        queue = deque([start])
        group: list[int] = []
        while queue:
            face_index = queue.popleft()
            group.append(face_index)
            for vertex in mesh.faces[face_index]:
                for adjacent in vertex_faces[vertex]:
                    if adjacent in pending:
                        pending.remove(adjacent)
                        queue.append(adjacent)
        face_groups.append(tuple(sorted(group)))
        if len(face_groups) > _MAX_COMPONENTS:
            return None
    result = []
    for group in face_groups:
        indices = tuple(sorted({vertex for face_index in group for vertex in mesh.faces[face_index]}))
        remap = {vertex: index for index, vertex in enumerate(indices)}
        hard_edges = frozenset(
            (remap[first], remap[second])
            for first, second in mesh.hard_edges
            if first in remap and second in remap
        )
        if any((first in remap) != (second in remap) for first, second in mesh.hard_edges):
            return None
        local = MeshData(
            tuple(mesh.vertices[vertex] for vertex in indices),
            tuple(tuple(remap[vertex] for vertex in mesh.faces[face_index]) for face_index in group),
            hard_edges,
        )
        result.append((local, tuple(density[vertex] for vertex in indices) if density else ()))
    return tuple(result)


def _assign_guides(
    guides: tuple[GuideCurveData, ...],
    components: tuple[tuple[MeshData, tuple[float, ...]], ...],
    cancelled: CancelledCallback | None,
) -> tuple[tuple[GuideCurveData, ...], ...] | None:
    assigned: list[list[GuideCurveData]] = [[] for _ in components]
    if not guides:
        return tuple(tuple(group) for group in assigned)
    indices = tuple(SurfaceIndex(_triangulate(mesh)) for mesh, _ in components)
    full_vertices = tuple(point for mesh, _ in components for point in mesh.vertices)
    extent = max(max(point[axis] for point in full_vertices) - min(point[axis] for point in full_vertices) for axis in range(3))
    tolerance = max(extent * 1.0e-5, 1.0e-8)
    for guide in guides:
        by_component: dict[int, list[int]] = defaultdict(list)
        for spline_index, spline in enumerate(guide.splines):
            if not spline:
                return None
            owners: set[int] = set()
            for point in spline:
                _check_cancelled(cancelled)
                distances = sorted((surface.nearest(point)[1], index) for index, surface in enumerate(indices))
                best, owner = distances[0]
                if best > max(extent * 0.1, tolerance):
                    return None
                if distances[1][0] - best <= tolerance:
                    return None
                owners.add(owner)
            if len(owners) != 1:
                return None
            by_component[owners.pop()].append(spline_index)
        for component_index, spline_indices in by_component.items():
            assigned[component_index].append(GuideCurveData(
                guide.name,
                tuple(guide.splines[index] for index in spline_indices),
                kind=tuple(guide.kind[index] for index in spline_indices),
                closed=tuple(guide.closed[index] for index in spline_indices),
            ))
    return tuple(tuple(group) for group in assigned)


def _area_budgets(
    components: tuple[tuple[MeshData, tuple[float, ...]], ...], total: int
) -> tuple[int, ...] | None:
    areas = tuple(_area(mesh) for mesh, _ in components)
    if any(area <= 1.0e-12 for area in areas):
        return None
    remainder = total - 4 * len(areas)
    total_area = sum(areas)
    shares = tuple(remainder * area / total_area for area in areas)
    budgets = [4 + int(share) for share in shares]
    remaining = total - sum(budgets)
    order = sorted(range(len(areas)), key=lambda index: (-(shares[index] - int(shares[index])), index))
    for index in order[:remaining]:
        budgets[index] += 1
    return tuple(budgets)


def _area(mesh: MeshData) -> float:
    area = 0.0
    for face in mesh.faces:
        origin = mesh.vertices[face[0]]
        for index in range(1, len(face) - 1):
            first = mesh.vertices[face[index]]
            second = mesh.vertices[face[index + 1]]
            cross = _cross(_sub(first, origin), _sub(second, origin))
            area += 0.5 * sqrt(sum(value * value for value in cross))
    return area


def _triangulate(mesh: MeshData) -> MeshData:
    triangles = tuple(
        (face[0], face[index], face[index + 1])
        for face in mesh.faces for index in range(1, len(face) - 1)
    )
    return MeshData(mesh.vertices, triangles)


def _preserves_component_topology(source: MeshData, output: MeshData) -> bool:
    try:
        output.validate()
    except ValueError:
        return False
    if any(len(face) != 4 or len(set(face)) != 4 for face in output.faces):
        return False
    if _component_count(output) != 1:
        return False
    source_boundaries = _boundary_loop_count(source)
    return source_boundaries >= 0 and source_boundaries == _boundary_loop_count(output)


def _component_count(mesh: MeshData) -> int:
    groups = _split_components(mesh, ())
    return len(groups) if groups is not None else -1


def _boundary_loop_count(mesh: MeshData) -> int:
    edge_faces: dict[tuple[int, int], int] = defaultdict(int)
    for face in mesh.faces:
        for first, second in zip(face, (*face[1:], face[0])):
            edge_faces[tuple(sorted((first, second)))] += 1
    if any(count > 2 for count in edge_faces.values()):
        return -1
    boundaries = tuple(edge for edge, count in edge_faces.items() if count == 1)
    if not boundaries:
        return 0
    neighbors: dict[int, set[int]] = defaultdict(set)
    for first, second in boundaries:
        neighbors[first].add(second)
        neighbors[second].add(first)
    if any(len(linked) != 2 for linked in neighbors.values()):
        return -1
    pending = set(neighbors)
    loops = 0
    while pending:
        loops += 1
        queue = deque([pending.pop()])
        while queue:
            for neighbor in neighbors[queue.popleft()] & pending:
                pending.remove(neighbor)
                queue.append(neighbor)
    return loops


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return tuple(first[index] - second[index] for index in range(3))  # type: ignore[return-value]


def _cross(first: Vector3, second: Vector3) -> Vector3:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("작업이 취소되었습니다.")
