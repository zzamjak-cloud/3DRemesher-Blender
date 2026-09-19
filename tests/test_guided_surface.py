from __future__ import annotations

import math
import unittest
from collections import defaultdict, deque
from statistics import median

from addon.core import (
    GuideCurveData,
    MeshData,
    RemeshCancelled,
    RemeshSettings,
    analyze_mesh,
    build_engine_input,
)
from addon.topology.guided_surface import try_remesh_guided_surface


class GuidedSurfaceTopologyTests(unittest.TestCase):
    def test_closed_head_accepts_three_independent_loop_guides(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        source = _triangulated(guide_mesh)
        guides = _face_guides(guide_mesh, by_coord)
        engine_input = build_engine_input(source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)

        result = try_remesh_guided_surface(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        source_analysis = analyze_mesh(source)
        analysis = analyze_mesh(result)
        self.assertEqual(source_analysis.triangle_count, len(source.faces))
        self.assertEqual(source_analysis.quad_count, 0)
        self.assertEqual(analysis.quad_count, len(result.faces))
        self.assertEqual(analysis.triangle_count, 0)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.degenerate_face_count, 0)
        self.assertEqual(len(result.faces), len(guide_mesh.faces))
        self.assertNotEqual(result.faces, source.faces)
        self.assertNotEqual(set(_edge_faces(result.faces)), set(_edge_faces(source.faces)))
        self.assertLess(_max_nearest_source_distance(result, guide_mesh), 0.08)
        _assert_quad_normals_follow_source(self, source, result)

        edge_faces = _edge_faces(result.faces)
        matched_cycles = []
        for guide in guides:
            cycle = _closed_cycle_near_guide(result, guide.splines[0])
            self.assertIsNotNone(cycle)
            assert cycle is not None
            self.assertGreaterEqual(len(cycle), 8)
            self.assertLess(_cycle_guide_error(result, cycle, guide.splines[0]), 0.025)
            self.assertTrue(_cycle_has_two_quad_bands(cycle, edge_faces, result.faces))
            matched_cycles.append(frozenset(cycle))

        self.assertEqual(len(set(matched_cycles)), 3)
        self.assertTrue(
            all(
                first.isdisjoint(second)
                for index, first in enumerate(matched_cycles)
                for second in matched_cycles[index + 1 :]
            )
        )

    def test_winding_follows_source_triangles_after_vertex_renumber_and_reverse(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        guides = _face_guides(guide_mesh, by_coord)
        source = _renumber_mesh(_triangulated(guide_mesh))
        reversed_source = _reverse_winding(source)

        result = try_remesh_guided_surface(
            build_engine_input(source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)
        )
        reversed_result = try_remesh_guided_surface(
            build_engine_input(reversed_source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)
        )

        self.assertIsNotNone(result)
        self.assertIsNotNone(reversed_result)
        assert result is not None
        assert reversed_result is not None
        _assert_quad_normals_follow_source(self, source, result)
        _assert_quad_normals_follow_source(self, reversed_source, reversed_result)
        self.assertLess(_dot(_face_normal(result.vertices, result.faces[0]), _face_normal(reversed_result.vertices, reversed_result.faces[0])), -0.99)

    def test_rejects_recovered_grid_when_target_count_is_far_away(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        source = _triangulated(guide_mesh)
        engine_input = build_engine_input(source, RemeshSettings(target_quad_count=100), guide_curves=_face_guides(guide_mesh, by_coord))

        self.assertIsNone(try_remesh_guided_surface(engine_input))

    def test_rejects_two_guides_that_share_the_same_surface_cycle(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        source = _triangulated(guide_mesh)
        shared_loop = _rectangle_loop((-6, 2), (-3, 5), 12)
        guides = (
            _loop_guide("curve_0", guide_mesh, by_coord, shared_loop),
            _loop_guide("curve_1", guide_mesh, by_coord, shared_loop),
            _loop_guide("curve_2", guide_mesh, by_coord, _rectangle_loop((-4, -5), (4, -3), 12)),
        )
        engine_input = build_engine_input(source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)

        self.assertIsNone(try_remesh_guided_surface(engine_input))

    def test_rejects_missing_face_layout_loop_set(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        source = _triangulated(guide_mesh)
        guides = (
            _loop_guide("curve_0", guide_mesh, by_coord, _rectangle_loop((-6, 2), (-3, 5), 12)),
            _loop_guide("curve_1", guide_mesh, by_coord, _rectangle_loop((-4, -5), (4, -3), 12)),
        )
        engine_input = build_engine_input(source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)

        self.assertIsNone(try_remesh_guided_surface(engine_input))

    def test_honors_cancellation(self):
        guide_mesh, by_coord = _rounded_head_mesh(12)
        source = _triangulated(guide_mesh)
        guides = (
            _loop_guide("curve_0", guide_mesh, by_coord, _rectangle_loop((-6, 2), (-3, 5), 12)),
            _loop_guide("curve_1", guide_mesh, by_coord, _rectangle_loop((3, 2), (6, 5), 12)),
            _loop_guide("curve_2", guide_mesh, by_coord, _rectangle_loop((-4, -5), (4, -3), 12)),
        )
        engine_input = build_engine_input(source, RemeshSettings(target_quad_count=len(guide_mesh.faces)), guide_curves=guides)

        with self.assertRaises(RemeshCancelled):
            try_remesh_guided_surface(engine_input, cancelled=lambda: True)


def _rounded_head_mesh(divisions: int) -> tuple[MeshData, dict[tuple[int, int, int], int]]:
    vertices: list[tuple[float, float, float]] = []
    by_coord: dict[tuple[int, int, int], int] = {}

    def vertex_index(coord: tuple[int, int, int]) -> int:
        if coord in by_coord:
            return by_coord[coord]
        x = coord[0] / divisions
        y = coord[1] / divisions
        z = coord[2] / divisions
        length = math.sqrt(x * x + y * y + z * z)
        vertex = (0.82 * x / length, 1.05 * y / length, 1.18 * z / length)
        by_coord[coord] = len(vertices)
        vertices.append(vertex)
        return by_coord[coord]

    faces: list[tuple[int, int, int, int]] = []

    def add_side(axis: int, sign: int) -> None:
        free_axes = [index for index in range(3) if index != axis]
        for first in range(-divisions, divisions):
            for second in range(-divisions, divisions):
                coords = []
                for offset_first, offset_second in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    coord = [0, 0, 0]
                    coord[axis] = sign * divisions
                    coord[free_axes[0]] = first + offset_first
                    coord[free_axes[1]] = second + offset_second
                    coords.append(tuple(coord))
                if (sign < 0) != (axis == 1):
                    coords.reverse()
                faces.append(tuple(vertex_index(coord) for coord in coords))

    for axis in range(3):
        add_side(axis, -1)
        add_side(axis, 1)
    return MeshData(vertices=tuple(vertices), faces=tuple(faces)), by_coord


def _rectangle_loop(
    lower_left: tuple[int, int],
    upper_right: tuple[int, int],
    z: int,
) -> tuple[tuple[int, int, int], ...]:
    x_min, y_min = lower_left
    x_max, y_max = upper_right
    points: list[tuple[int, int, int]] = []
    for x in range(x_min, x_max + 1):
        points.append((x, y_min, z))
    for y in range(y_min + 1, y_max + 1):
        points.append((x_max, y, z))
    for x in range(x_max - 1, x_min - 1, -1):
        points.append((x, y_max, z))
    for y in range(y_max - 1, y_min, -1):
        points.append((x_min, y, z))
    return tuple(points)


def _loop_guide(
    name: str,
    mesh: MeshData,
    by_coord: dict[tuple[int, int, int], int],
    coords: tuple[tuple[int, int, int], ...],
) -> GuideCurveData:
    points = tuple(mesh.vertices[by_coord[coord]] for coord in coords)
    return GuideCurveData(name=name, splines=(points + (points[0],),), kind=("LOOP",), closed=(True,))


def _face_guides(mesh: MeshData, by_coord: dict[tuple[int, int, int], int]) -> tuple[GuideCurveData, ...]:
    return (
        _loop_guide("feature_a", mesh, by_coord, _rectangle_loop((-6, 2), (-3, 5), 12)),
        _loop_guide("feature_b", mesh, by_coord, _rectangle_loop((3, 2), (6, 5), 12)),
        _loop_guide("feature_c", mesh, by_coord, _rectangle_loop((-4, -5), (4, -3), 12)),
    )


def _triangulated(mesh: MeshData) -> MeshData:
    faces = []
    for first, second, third, fourth in mesh.faces:
        faces.append((first, second, third))
        faces.append((first, third, fourth))
    return MeshData(mesh.vertices, tuple(faces))


def _renumber_mesh(mesh: MeshData) -> MeshData:
    old_indices = tuple(range(len(mesh.vertices)))
    new_to_old = old_indices[1::2] + old_indices[::2]
    old_to_new = {old: new for new, old in enumerate(new_to_old)}
    vertices = tuple(mesh.vertices[old] for old in new_to_old)
    faces = tuple(tuple(old_to_new[index] for index in face) for face in mesh.faces)
    return MeshData(vertices, faces)


def _reverse_winding(mesh: MeshData) -> MeshData:
    return MeshData(mesh.vertices, tuple(tuple(reversed(face)) for face in mesh.faces))


def _assert_quad_normals_follow_source(test_case: unittest.TestCase, source: MeshData, result: MeshData) -> None:
    test_case.assertEqual(len(source.faces), len(result.faces) * 2)
    for quad_index, face in enumerate(result.faces):
        source_normal = _face_normal(source.vertices, source.faces[quad_index * 2])
        result_normal = _face_normal(result.vertices, face)
        test_case.assertGreater(_dot(source_normal, result_normal), 0.99)


def _edge_faces(faces: tuple[tuple[int, ...], ...]) -> dict[tuple[int, int], tuple[int, ...]]:
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for index, first in enumerate(face):
            second = face[(index + 1) % len(face)]
            edge_faces[_edge_key(first, second)].append(face_index)
    return {edge: tuple(face_indices) for edge, face_indices in edge_faces.items()}


def _closed_cycle_near_guide(mesh: MeshData, guide: tuple[tuple[float, float, float], ...]) -> tuple[int, ...] | None:
    edge_length = median(
        _distance(mesh.vertices[first], mesh.vertices[second])
        for first, second in _edge_faces(mesh.faces)
    )
    guide_points = guide[:-1] if _distance(guide[0], guide[-1]) < 1.0e-9 else guide
    tolerance = edge_length * 0.45
    candidate_vertices = {
        index
        for index, vertex in enumerate(mesh.vertices)
        if _distance_to_polyline(vertex, guide_points) <= tolerance
    }
    adjacency: dict[int, set[int]] = defaultdict(set)
    for first, second in _edge_faces(mesh.faces):
        if first in candidate_vertices and second in candidate_vertices:
            adjacency[first].add(second)
            adjacency[second].add(first)
    components = _components(adjacency)
    cycles = [component for component in components if len(component) >= 4 and all(len(adjacency[v].intersection(component)) == 2 for v in component)]
    if len(cycles) != 1:
        return None
    return _order_cycle(cycles[0], adjacency)


def _cycle_has_two_quad_bands(
    cycle: tuple[int, ...],
    edge_faces: dict[tuple[int, int], tuple[int, ...]],
    faces: tuple[tuple[int, ...], ...],
) -> bool:
    band_faces = []
    for index, first in enumerate(cycle):
        second = cycle[(index + 1) % len(cycle)]
        attached = edge_faces.get(_edge_key(first, second))
        if attached is None or len(attached) != 2:
            return False
        if any(len(faces[face_index]) != 4 for face_index in attached):
            return False
        band_faces.extend(attached)
    return len(set(band_faces)) >= len(cycle)


def _components(adjacency: dict[int, set[int]]) -> tuple[frozenset[int], ...]:
    remaining = set(adjacency)
    result = []
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
        result.append(frozenset(component))
    return tuple(result)


def _order_cycle(component: frozenset[int], adjacency: dict[int, set[int]]) -> tuple[int, ...] | None:
    start = min(component)
    ordered = [start]
    previous = None
    current = start
    while True:
        neighbors = sorted(adjacency[current].intersection(component))
        next_vertex = neighbors[0] if neighbors[0] != previous else neighbors[1]
        if next_vertex == start:
            return tuple(ordered) if len(ordered) == len(component) else None
        if next_vertex in ordered:
            return None
        ordered.append(next_vertex)
        previous, current = current, next_vertex


def _cycle_guide_error(
    mesh: MeshData,
    cycle: tuple[int, ...],
    guide: tuple[tuple[float, float, float], ...],
) -> float:
    guide_points = guide[:-1] if _distance(guide[0], guide[-1]) < 1.0e-9 else guide
    return max(_distance_to_polyline(mesh.vertices[index], guide_points) for index in cycle)


def _max_nearest_source_distance(result: MeshData, source: MeshData) -> float:
    return max(min(_distance(vertex, source_vertex) for source_vertex in source.vertices) for vertex in result.vertices)


def _distance_to_polyline(point: tuple[float, float, float], points: tuple[tuple[float, float, float], ...]) -> float:
    return min(_distance_to_segment(point, first, points[(index + 1) % len(points)]) for index, first in enumerate(points))


def _distance_to_segment(
    point: tuple[float, float, float],
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    segment = (second[0] - first[0], second[1] - first[1], second[2] - first[2])
    length_squared = segment[0] * segment[0] + segment[1] * segment[1] + segment[2] * segment[2]
    if length_squared <= 1.0e-12:
        return _distance(point, first)
    projected = (
        (point[0] - first[0]) * segment[0]
        + (point[1] - first[1]) * segment[1]
        + (point[2] - first[2]) * segment[2]
    )
    t = max(0.0, min(1.0, projected / length_squared))
    closest = (first[0] + segment[0] * t, first[1] + segment[1] * t, first[2] + segment[2] * t)
    return _distance(point, closest)


def _face_normal(
    vertices: tuple[tuple[float, float, float], ...],
    face: tuple[int, ...],
) -> tuple[float, float, float]:
    normal = (0.0, 0.0, 0.0)
    for index, vertex_index in enumerate(face):
        current = vertices[vertex_index]
        following = vertices[face[(index + 1) % len(face)]]
        normal = (
            normal[0] + (current[1] - following[1]) * (current[2] + following[2]),
            normal[1] + (current[2] - following[2]) * (current[0] + following[0]),
            normal[2] + (current[0] - following[0]) * (current[1] + following[1]),
        )
    length = math.sqrt(_dot(normal, normal))
    return (normal[0] / length, normal[1] / length, normal[2] / length)


def _dot(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(
        (first[0] - second[0]) * (first[0] - second[0])
        + (first[1] - second[1]) * (first[1] - second[1])
        + (first[2] - second[2]) * (first[2] - second[2])
    )


if __name__ == "__main__":
    unittest.main()
