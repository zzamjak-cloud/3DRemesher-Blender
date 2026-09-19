from __future__ import annotations

from itertools import product
from math import isclose
from typing import Iterable, Sequence

from .core import MeshData, Vector3


EPSILON = 1.0e-9
SNAP_RELATIVE_EPSILON = 1.0e-6
AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
CANONICAL_AXES = ("X", "Y", "Z")


def clip_to_symmetry(
    mesh: MeshData,
    density_values: tuple[float, ...],
    axes: tuple[str, ...],
) -> tuple[MeshData, tuple[float, ...]]:
    mesh.validate()
    canonical_axes = _canonical_axes(axes)
    if density_values and len(density_values) != len(mesh.vertices):
        raise ValueError("밀도 값 개수는 메시 정점 개수와 같아야 합니다.")
    if not canonical_axes:
        return mesh, density_values

    clipped_mesh = mesh
    clipped_density = density_values
    tolerance = _mesh_tolerance(mesh)
    for axis in canonical_axes:
        clipped_mesh, clipped_density = _clip_one_axis(clipped_mesh, clipped_density, axis, tolerance)
    return clipped_mesh, clipped_density


def mirror_symmetry(mesh: MeshData, axes: tuple[str, ...]) -> MeshData:
    mesh.validate()
    canonical_axes = _canonical_axes(axes)
    if not canonical_axes:
        return mesh

    tolerance = _mesh_tolerance(mesh)
    axis_indices = tuple(AXIS_INDEX[axis] for axis in canonical_axes)
    vertices: list[Vector3] = []
    vertex_map: dict[tuple[int, tuple[tuple[int, float], ...]], int] = {}
    faces: list[tuple[int, ...]] = []
    face_keys: set[tuple[int, ...]] = set()
    hard_edges: set[tuple[int, int]] = set()

    for signs in product((1.0, -1.0), repeat=len(axis_indices)):
        transform = {axis_index: sign for axis_index, sign in zip(axis_indices, signs)}
        odd_reflection = sum(1 for sign in signs if sign < 0.0) % 2 == 1
        index_map: dict[int, int] = {}

        for source_index, vertex in enumerate(mesh.vertices):
            mirrored = _mirror_vertex(vertex, transform)
            key = _mirror_vertex_key(source_index, vertex, transform, tolerance)
            output_index = vertex_map.get(key)
            if output_index is None:
                output_index = len(vertices)
                vertex_map[key] = output_index
                vertices.append(_clean_vertex(mirrored))
            index_map[source_index] = output_index

        for face in mesh.faces:
            mirrored_face = tuple(index_map[index] for index in face)
            if len(set(mirrored_face)) < 3:
                continue
            if odd_reflection:
                mirrored_face = tuple(reversed(mirrored_face))
            face_key = tuple(sorted(mirrored_face))
            if face_key in face_keys:
                continue
            face_keys.add(face_key)
            faces.append(mirrored_face)

        for first, second in mesh.hard_edges:
            mapped_edge = _edge_key(index_map[first], index_map[second])
            if mapped_edge[0] != mapped_edge[1]:
                hard_edges.add(mapped_edge)

    return MeshData(vertices=tuple(vertices), faces=tuple(faces), hard_edges=frozenset(hard_edges))


def symmetry_error(mesh: MeshData, axes: tuple[str, ...]) -> float:
    mesh.validate()
    canonical_axes = _canonical_axes(axes)
    if not canonical_axes:
        return 0.0

    vertex_keys = {_vertex_key(vertex) for vertex in mesh.vertices}
    worst = 0.0
    for vertex in mesh.vertices:
        for axis in canonical_axes:
            mirrored = list(vertex)
            mirrored[AXIS_INDEX[axis]] = -mirrored[AXIS_INDEX[axis]]
            key = _vertex_key(tuple(mirrored))
            if key not in vertex_keys:
                worst = max(worst, abs(vertex[AXIS_INDEX[axis]]))
    return worst


def _clip_one_axis(mesh: MeshData, density_values: tuple[float, ...], axis: str, tolerance: float) -> tuple[MeshData, tuple[float, ...]]:
    axis_index = AXIS_INDEX[axis]
    output_vertices = [_snap_axis(tuple(vertex), axis_index, tolerance) for vertex in mesh.vertices]
    output_density = list(density_values) if density_values else []
    edge_intersections: dict[tuple[int, int], int] = {}
    output_faces: list[tuple[int, ...]] = []
    seam_edges: set[tuple[int, int]] = set()
    hard_fragments: set[tuple[int, int]] = set()
    source_hard_edges = {_edge_key(first, second) for first, second in mesh.hard_edges}

    for face in mesh.faces:
        clipped = _clip_face(
            face,
            output_vertices,
            output_density,
            density_values,
            edge_intersections,
            hard_fragments,
            source_hard_edges,
            axis_index,
            tolerance,
        )
        if len(clipped) >= 3:
            clean_face = _cleanup_face(clipped, output_vertices, tolerance)
            if len(clean_face) >= 3 and _polygon_area(output_vertices, clean_face) > EPSILON:
                output_faces.append(clean_face)
                for first, second in _face_edges(clean_face):
                    if _on_axis_plane(output_vertices[first], axis_index, tolerance) and _on_axis_plane(output_vertices[second], axis_index, tolerance):
                        seam_edges.add(_edge_key(first, second))

    used_indices = sorted({index for face in output_faces for index in face})
    if not output_faces or not used_indices:
        raise ValueError(f"{axis} 대칭 기준의 양수 영역에 남는 면이 없습니다.")

    remap = {old_index: new_index for new_index, old_index in enumerate(used_indices)}
    compact_vertices = tuple(output_vertices[index] for index in used_indices)
    compact_faces = tuple(tuple(remap[index] for index in face) for face in output_faces)
    compact_density = tuple(output_density[index] for index in used_indices) if density_values else ()

    hard_edges = {_edge_key(remap[first], remap[second]) for first, second in hard_fragments if first in remap and second in remap}
    hard_edges.update(_edge_key(remap[first], remap[second]) for first, second in seam_edges)

    return MeshData(compact_vertices, compact_faces, frozenset(hard_edges)), compact_density


def _clip_face(
    face: Sequence[int],
    vertices: list[Vector3],
    output_density: list[float],
    source_density: tuple[float, ...],
    edge_intersections: dict[tuple[int, int], int],
    hard_fragments: set[tuple[int, int]],
    source_hard_edges: set[tuple[int, int]],
    axis_index: int,
    tolerance: float,
) -> list[int]:
    result: list[int] = []
    previous = face[-1]
    previous_inside = _inside(vertices[previous], axis_index, tolerance)

    for current in face:
        current_inside = _inside(vertices[current], axis_index, tolerance)
        if current_inside:
            if not previous_inside:
                result.append(
                    _intersection_vertex(
                        previous,
                        current,
                        vertices,
                        output_density,
                        source_density,
                        edge_intersections,
                        hard_fragments,
                        source_hard_edges,
                        axis_index,
                        tolerance,
                    )
                )
            result.append(current)
            if previous_inside and _edge_key(previous, current) in source_hard_edges:
                hard_fragments.add(_edge_key(previous, current))
        elif previous_inside:
            result.append(
                _intersection_vertex(
                    previous,
                    current,
                    vertices,
                    output_density,
                    source_density,
                    edge_intersections,
                    hard_fragments,
                    source_hard_edges,
                    axis_index,
                    tolerance,
                )
            )
        previous = current
        previous_inside = current_inside

    return result


def _intersection_vertex(
    first: int,
    second: int,
    vertices: list[Vector3],
    output_density: list[float],
    source_density: tuple[float, ...],
    edge_intersections: dict[tuple[int, int], int],
    hard_fragments: set[tuple[int, int]],
    source_hard_edges: set[tuple[int, int]],
    axis_index: int,
    tolerance: float,
) -> int:
    first_value = vertices[first][axis_index]
    second_value = vertices[second][axis_index]
    if _on_axis_plane(vertices[first], axis_index, tolerance):
        if _edge_key(first, second) in source_hard_edges:
            hard_fragments.add(_edge_key(first, second))
        return first
    if _on_axis_plane(vertices[second], axis_index, tolerance):
        if _edge_key(first, second) in source_hard_edges:
            hard_fragments.add(_edge_key(first, second))
        return second

    key = _edge_key(first, second)
    cached = edge_intersections.get(key)
    if cached is not None:
        if key in source_hard_edges:
            hard_fragments.add(_edge_key(first, cached))
            hard_fragments.add(_edge_key(cached, second))
        return cached

    denominator = first_value - second_value
    if isclose(denominator, 0.0, abs_tol=EPSILON):
        raise ValueError("클리핑 교점을 계산할 수 없는 엣지입니다.")
    ratio = first_value / denominator
    point = tuple(vertices[first][component] + (vertices[second][component] - vertices[first][component]) * ratio for component in range(3))
    point = _snap_axis(_replace_component(point, axis_index, 0.0), axis_index, tolerance)

    index = len(vertices)
    vertices.append(_clean_vertex(point))
    if source_density:
        density = output_density[first] + (output_density[second] - output_density[first]) * ratio
        output_density.append(density)
    edge_intersections[key] = index
    if key in source_hard_edges:
        hard_fragments.add(_edge_key(first, index))
        hard_fragments.add(_edge_key(index, second))
    return index


def _canonical_axes(axes: Iterable[str]) -> tuple[str, ...]:
    axis_set = set(axes)
    invalid_axes = sorted(axis_set - set(CANONICAL_AXES))
    if invalid_axes:
        raise ValueError(f"지원하지 않는 대칭 축입니다: {', '.join(invalid_axes)}")
    return tuple(axis for axis in CANONICAL_AXES if axis in axis_set)


def _mirror_vertex(vertex: Vector3, transform: dict[int, float]) -> Vector3:
    values = list(vertex)
    for axis_index, sign in transform.items():
        values[axis_index] *= sign
    return tuple(values)


def _mirror_vertex_key(source_index: int, vertex: Vector3, transform: dict[int, float], tolerance: float) -> tuple[int, tuple[tuple[int, float], ...]]:
    signs = []
    for axis_index, sign in sorted(transform.items()):
        if _on_axis_plane(vertex, axis_index, tolerance):
            continue
        signs.append((axis_index, sign))
    return source_index, tuple(signs)


def _inside(vertex: Vector3, axis_index: int, tolerance: float) -> bool:
    return vertex[axis_index] >= -tolerance


def _on_axis_plane(vertex: Vector3, axis_index: int, tolerance: float = EPSILON) -> bool:
    return abs(vertex[axis_index]) <= tolerance


def _replace_component(vertex: Vector3, axis_index: int, value: float) -> Vector3:
    values = list(vertex)
    values[axis_index] = value
    return tuple(values)


def _cleanup_face(indices: Sequence[int], vertices: Sequence[Vector3], tolerance: float) -> tuple[int, ...]:
    cleaned: list[int] = []
    for index in indices:
        if not cleaned or cleaned[-1] != index:
            cleaned.append(index)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    changed = True
    while changed and len(cleaned) >= 3:
        changed = False
        next_cleaned: list[int] = []
        count = len(cleaned)
        for offset, index in enumerate(cleaned):
            previous_index = cleaned[offset - 1]
            next_index = cleaned[(offset + 1) % count]
            if _triangle_area(vertices[previous_index], vertices[index], vertices[next_index]) <= tolerance * tolerance:
                changed = True
                continue
            next_cleaned.append(index)
        cleaned = next_cleaned
    return tuple(cleaned)


def _face_edges(face: Sequence[int]) -> Iterable[tuple[int, int]]:
    return zip(face, (*face[1:], face[0]))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _vertex_key(vertex: Vector3) -> tuple[int, int, int]:
    return tuple(0 if abs(component) <= EPSILON else round(component / EPSILON) for component in vertex)


def _clean_vertex(vertex: Vector3) -> Vector3:
    return tuple(0.0 if abs(component) <= EPSILON else float(component) for component in vertex)


def _snap_axis(vertex: Vector3, axis_index: int, tolerance: float) -> Vector3:
    if not _on_axis_plane(vertex, axis_index, tolerance):
        return vertex
    values = list(vertex)
    values[axis_index] = 0.0
    return tuple(values)


def _mesh_tolerance(mesh: MeshData) -> float:
    extent = 0.0
    for axis_index in range(3):
        values = [vertex[axis_index] for vertex in mesh.vertices]
        extent = max(extent, max(values) - min(values))
    return max(EPSILON, extent * SNAP_RELATIVE_EPSILON)


def _polygon_area(vertices: Sequence[Vector3], face: Sequence[int]) -> float:
    if len(face) < 3:
        return 0.0
    origin = vertices[face[0]]
    area = 0.0
    for index in range(1, len(face) - 1):
        area += _triangle_area(origin, vertices[face[index]], vertices[face[index + 1]])
    return area


def _triangle_area(a: Vector3, b: Vector3, c: Vector3) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return 0.5 * (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5
