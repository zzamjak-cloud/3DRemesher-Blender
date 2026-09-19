from __future__ import annotations

import unittest

from addon.adaptive import adapt_triangles
from addon.core import MeshData, RemeshCancelled, analyze_mesh


class AdaptiveTriangleTests(unittest.TestCase):
    def test_refuses_octahedron_reduction_that_exceeds_shape_error(self):
        mesh = _octahedron()

        result, densities, warnings = adapt_triangles(mesh, 4)

        analysis = analyze_mesh(result)
        self.assertEqual(analysis.face_count, 8)
        self.assertEqual(analysis.triangle_count, 8)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.degenerate_face_count, 0)
        self.assertEqual(len(densities), len(result.vertices))
        self.assertTrue(any("target=4" in warning for warning in warnings))

    def test_reduces_planar_grid_while_preserving_boundary_hard_edges(self):
        mesh = _grid_mesh(4, 4, hard_boundary=True)

        result, _, warnings = adapt_triangles(mesh, 24)

        analysis = analyze_mesh(result)
        self.assertEqual(analysis.face_count, 24)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertTrue(mesh.hard_edges)
        self.assertTrue(result.hard_edges)
        for corner in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)):
            self.assertIn(corner, result.vertices)
        self.assertEqual(warnings, ())

    def test_reduces_collinear_subdivided_feature_cube(self):
        mesh = _cube_surface_grid(4)

        result, _, warnings = adapt_triangles(mesh, 64)

        analysis = analyze_mesh(result)
        self.assertEqual(analysis.face_count, 64)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.degenerate_face_count, 0)
        self.assertLess(len(result.hard_edges), len(mesh.hard_edges))
        for corner in ((-1, -1, -1), (1, -1, -1), (1, 1, 1), (-1, 1, 1)):
            self.assertIn(corner, result.vertices)
        self.assertEqual(warnings, ())

    def test_reduces_dense_sphere_arc_seam_without_keeping_all_samples(self):
        mesh = _arc_strip_mesh(16)

        result, _, warnings = adapt_triangles(mesh, 16)

        analysis = analyze_mesh(result)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.degenerate_face_count, 0)
        self.assertLessEqual(analysis.face_count, 24)
        self.assertLess(len(result.hard_edges), len(mesh.hard_edges))
        self.assertTrue(any("target=16" in warning for warning in warnings))

    def test_symmetry_seam_on_plane_stays_on_axis(self):
        mesh = _axis_seam_grid()

        result, _, _ = adapt_triangles(mesh, 18)

        hard_vertices = {vertex for edge in result.hard_edges for vertex in edge}
        self.assertTrue(hard_vertices)
        for vertex_index in hard_vertices:
            self.assertAlmostEqual(result.vertices[vertex_index][0], 0.0)

    def test_increases_triangles_with_conforming_splits(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
            hard_edges=frozenset({(0, 1)}),
        )

        result, densities, warnings = adapt_triangles(mesh, 4, (1.0, 2.0, 1.0))

        analysis = analyze_mesh(result)
        self.assertEqual(analysis.face_count, 4)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(len(densities), len(result.vertices))
        self.assertTrue(any(0 in edge or 1 in edge for edge in result.hard_edges))
        self.assertEqual(warnings, ())

    def test_density_keeps_more_faces_in_weighted_region(self):
        mesh = _grid_mesh(5, 3, hard_boundary=True)
        uniform_density = tuple(1.0 for _ in mesh.vertices)
        left_density = tuple(8.0 if vertex[0] < 0.5 else 0.2 for vertex in mesh.vertices)

        uniform_result, _, _ = adapt_triangles(mesh, 20, uniform_density)
        weighted_result, _, _ = adapt_triangles(mesh, 20, left_density)

        self.assertGreaterEqual(
            _count_faces_left_of(weighted_result, 0.5),
            _count_faces_left_of(uniform_result, 0.5),
        )

    def test_density_redistributes_when_target_matches_source_count(self):
        mesh = _grid_mesh(5, 3, hard_boundary=True)
        left_density = tuple(8.0 if vertex[0] < 0.5 else 0.2 for vertex in mesh.vertices)

        result, _, warnings = adapt_triangles(mesh, len(mesh.faces), left_density)

        self.assertEqual(len(result.faces), len(mesh.faces))
        self.assertGreater(_count_faces_left_of(result, 0.5), _count_faces_left_of(mesh, 0.5))
        self.assertEqual(warnings, ())

    def test_torus_density_keeps_more_faces_on_high_density_side(self):
        mesh = _torus_mesh(48, 16)
        target = 64
        uniform_density = tuple(1.0 for _ in mesh.vertices)
        right_density = tuple(1.0 if vertex[0] >= 0.0 else 0.2 for vertex in mesh.vertices)

        uniform_result, _, _ = adapt_triangles(mesh, target, uniform_density, density_scale=2.0)
        density_result, _, _ = adapt_triangles(mesh, target, right_density, density_scale=2.0)

        uniform_ratio = _count_faces_positive_x(uniform_result) / max(1, _count_faces_negative_x(uniform_result))
        density_ratio = _count_faces_positive_x(density_result) / max(1, _count_faces_negative_x(density_result))
        self.assertGreaterEqual(analyze_mesh(density_result).face_count, target)
        self.assertLess(len(density_result.faces),len(mesh.faces))
        # 형상 제약 아래에서도 고밀도 영역의 배분은 실제로 증가해야 한다.
        self.assertGreater(density_ratio, uniform_ratio * 1.1)
        from math import sqrt
        deviations=[abs(sqrt((sqrt(v[0]**2+v[1]**2)-1.)**2+v[2]**2)-.32) for v in density_result.vertices]
        self.assertLess(max(deviations), .07)

    def test_accumulated_quadric_minimizes_original_plane_error(self):
        from addon.adaptive import _plane_quadric, _add_quadrics, _best_edge_point, _quadric_error
        q=_add_quadrics(_plane_quadric((0.,0.,1.,0.)),_plane_quadric((0.,0.,1.,-.1)))
        point=_best_edge_point((.2,.3,0.),(.2,.3,.2),q)
        self.assertAlmostEqual(point[2],.05)
        self.assertAlmostEqual(_quadric_error(q,point),.005)
        self.assertEqual(q[10],2.)

    def test_feature_edge_is_preserved_through_increase(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2), (0, 2, 3)),
            hard_edges=frozenset({(0, 2)}),
        )

        result, _, _ = adapt_triangles(mesh, 6)

        hard_vertices = {vertex for edge in result.hard_edges for vertex in edge}
        self.assertIn(0, hard_vertices)
        self.assertIn(2, hard_vertices)
        self.assertGreaterEqual(len(result.hard_edges), 2)

    def test_cancellation_leaves_input_unchanged(self):
        mesh = _grid_mesh(4, 4, hard_boundary=True)
        original = mesh
        progress_events: list[tuple[float, str]] = []

        def progress(fraction: float, message: str) -> None:
            progress_events.append((fraction, message))

        with self.assertRaises(RemeshCancelled):
            adapt_triangles(mesh, 16, cancelled=lambda: bool(progress_events), progress=progress)

        self.assertEqual(mesh, original)

    def test_rejects_invalid_or_non_triangle_input(self):
        quad_mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2, 3),),
        )
        with self.assertRaisesRegex(ValueError, "삼각형 메시"):
            adapt_triangles(quad_mesh, 2)

        duplicate = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2), (2, 1, 0)),
        )
        with self.assertRaisesRegex(ValueError, "중복 면"):
            adapt_triangles(duplicate, 1)


def _octahedron() -> MeshData:
    return MeshData(
        vertices=((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)),
        faces=((0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4), (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)),
    )


def _grid_mesh(columns: int, rows: int, *, hard_boundary: bool) -> MeshData:
    vertices = []
    for y in range(rows + 1):
        for x in range(columns + 1):
            vertices.append((x / columns, y / rows, 0.0))

    def index(x: int, y: int) -> int:
        return y * (columns + 1) + x

    faces = []
    for y in range(rows):
        for x in range(columns):
            a = index(x, y)
            b = index(x + 1, y)
            c = index(x + 1, y + 1)
            d = index(x, y + 1)
            faces.append((a, b, c))
            faces.append((a, c, d))

    hard_edges = set()
    if hard_boundary:
        for x in range(columns):
            hard_edges.add(tuple(sorted((index(x, 0), index(x + 1, 0)))))
            hard_edges.add(tuple(sorted((index(x, rows), index(x + 1, rows)))))
        for y in range(rows):
            hard_edges.add(tuple(sorted((index(0, y), index(0, y + 1)))))
            hard_edges.add(tuple(sorted((index(columns, y), index(columns, y + 1)))))

    return MeshData(vertices=tuple(vertices), faces=tuple(faces), hard_edges=frozenset(hard_edges))


def _cube_surface_grid(divisions: int) -> MeshData:
    vertices = []
    vertex_indices = {}

    def add_vertex(point):
        key = tuple(round(value, 6) for value in point)
        if key not in vertex_indices:
            vertex_indices[key] = len(vertices)
            vertices.append(key)
        return vertex_indices[key]

    def lerp(first, second, ratio):
        return tuple(first[index] + (second[index] - first[index]) * ratio for index in range(3))

    def bilinear(corner0, corner1, corner2, corner3, u, v):
        lower = lerp(corner0, corner1, u)
        upper = lerp(corner3, corner2, u)
        return lerp(lower, upper, v)

    cube_faces = (
        ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)),
        ((-1, -1, -1), (-1, 1, -1), (1, 1, -1), (1, -1, -1)),
        ((-1, 1, -1), (-1, 1, 1), (1, 1, 1), (1, 1, -1)),
        ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)),
        ((1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)),
        ((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)),
    )
    faces = []
    for corners in cube_faces:
        grid = []
        for y in range(divisions + 1):
            row = []
            for x in range(divisions + 1):
                row.append(add_vertex(bilinear(*corners, x / divisions, y / divisions)))
            grid.append(row)
        for y in range(divisions):
            for x in range(divisions):
                a = grid[y][x]
                b = grid[y][x + 1]
                c = grid[y + 1][x + 1]
                d = grid[y + 1][x]
                faces.append((a, b, c))
                faces.append((a, c, d))

    hard_edges = set()
    for face in faces:
        for first, second in zip(face, (*face[1:], face[0])):
            first_vertex = vertices[first]
            second_vertex = vertices[second]
            common_boundary_axes = sum(
                1
                for axis in range(3)
                if abs(first_vertex[axis]) == 1 and first_vertex[axis] == second_vertex[axis]
            )
            if common_boundary_axes >= 2:
                hard_edges.add(tuple(sorted((first, second))))

    return MeshData(vertices=tuple(vertices), faces=tuple(faces), hard_edges=frozenset(hard_edges))


def _arc_strip_mesh(segments: int) -> MeshData:
    import math

    vertices = []
    for index in range(segments + 1):
        angle = (math.pi * 0.5) * index / segments
        vertices.append((math.cos(angle), math.sin(angle), 0.0))
    for index in range(segments + 1):
        angle = (math.pi * 0.5) * index / segments
        vertices.append((1.2 * math.cos(angle), 1.2 * math.sin(angle), 0.0))

    def inner(index):
        return index

    def outer(index):
        return segments + 1 + index

    faces = []
    for index in range(segments):
        faces.append((inner(index), outer(index), outer(index + 1)))
        faces.append((inner(index), outer(index + 1), inner(index + 1)))
    hard_edges = {tuple(sorted((inner(index), inner(index + 1)))) for index in range(segments)}
    return MeshData(vertices=tuple(vertices), faces=tuple(faces), hard_edges=frozenset(hard_edges))


def _axis_seam_grid() -> MeshData:
    mesh = _grid_mesh(4, 4, hard_boundary=False)
    hard_edges = set()

    def index(x: int, y: int) -> int:
        return y * 5 + x

    for y in range(4):
        hard_edges.add(tuple(sorted((index(2, y), index(2, y + 1)))))
    remapped_vertices = tuple((vertex[0] * 2.0 - 1.0, vertex[1], vertex[2]) for vertex in mesh.vertices)
    return MeshData(vertices=remapped_vertices, faces=mesh.faces, hard_edges=frozenset(hard_edges))


def _torus_mesh(major_segments: int, minor_segments: int) -> MeshData:
    import math

    vertices = []
    major_radius = 1.0
    minor_radius = 0.32
    for major_index in range(major_segments):
        major_angle = 2.0 * math.pi * major_index / major_segments
        major_center = (math.cos(major_angle), math.sin(major_angle), 0.0)
        for minor_index in range(minor_segments):
            minor_angle = 2.0 * math.pi * minor_index / minor_segments
            radial = major_radius + minor_radius * math.cos(minor_angle)
            vertices.append(
                (
                    radial * major_center[0],
                    radial * major_center[1],
                    minor_radius * math.sin(minor_angle),
                )
            )

    def index(major_index: int, minor_index: int) -> int:
        return (major_index % major_segments) * minor_segments + (minor_index % minor_segments)

    faces = []
    for major_index in range(major_segments):
        for minor_index in range(minor_segments):
            a = index(major_index, minor_index)
            b = index(major_index + 1, minor_index)
            c = index(major_index + 1, minor_index + 1)
            d = index(major_index, minor_index + 1)
            faces.append((a, b, c))
            faces.append((a, c, d))
    return MeshData(vertices=tuple(vertices), faces=tuple(faces))


def _count_faces_left_of(mesh: MeshData, threshold: float) -> int:
    count = 0
    for face in mesh.faces:
        centroid_x = sum(mesh.vertices[index][0] for index in face) / 3.0
        if centroid_x < threshold:
            count += 1
    return count


def _count_faces_positive_x(mesh: MeshData) -> int:
    return sum(1 for face in mesh.faces if sum(mesh.vertices[index][0] for index in face) / 3.0 >= 0.0)


def _count_faces_negative_x(mesh: MeshData) -> int:
    return sum(1 for face in mesh.faces if sum(mesh.vertices[index][0] for index in face) / 3.0 < 0.0)


if __name__ == "__main__":
    unittest.main()
