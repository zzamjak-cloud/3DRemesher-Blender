from __future__ import annotations

import math
import unittest

from addon.core import MeshData, analyze_mesh
from addon.engine import _validate_topology
from addon.symmetry import clip_to_symmetry, mirror_symmetry, symmetry_error


class SymmetryTests(unittest.TestCase):
    def test_clip_x_interpolates_density_and_preserves_hard_edge_fragment(self):
        mesh = MeshData(
            vertices=((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            faces=((0, 1, 2),),
            hard_edges=frozenset({(0, 1)}),
        )

        clipped, density = clip_to_symmetry(mesh, (0.0, 2.0, 4.0), ("X",))

        self.assertEqual(len(clipped.faces), 1)
        self.assertTrue(all(vertex[0] >= -1.0e-9 for vertex in clipped.vertices))
        self.assertIn(1.0, density)
        self.assertTrue(any(_edge_points(clipped, edge) == {(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)} for edge in clipped.hard_edges))
        self.assertEqual(mesh.vertices[0], (-1.0, 0.0, 0.0))

    def test_clip_y_and_z_keep_positive_half(self):
        mesh = MeshData(
            vertices=((0.0, -1.0, -1.0), (0.0, 1.0, -1.0), (0.0, 1.0, 1.0), (0.0, -1.0, 1.0)),
            faces=((0, 1, 2, 3),),
        )

        clipped_y, _ = clip_to_symmetry(mesh, (), ("Y",))
        clipped_z, _ = clip_to_symmetry(mesh, (), ("Z",))

        self.assertTrue(all(vertex[1] >= -1.0e-9 for vertex in clipped_y.vertices))
        self.assertTrue(all(vertex[2] >= -1.0e-9 for vertex in clipped_z.vertices))
        self.assertGreater(_surface_area(clipped_y), 0.0)
        self.assertGreater(_surface_area(clipped_z), 0.0)

    def test_clip_multiple_axes_is_canonical_and_rejects_empty_positive_side(self):
        mesh = MeshData(
            vertices=((-2.0, -2.0, 0.0), (-1.0, -2.0, 0.0), (-1.0, -1.0, 0.0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "양수 영역"):
            clip_to_symmetry(mesh, (), ("Y", "X"))

    def test_mirror_x_plane_faces_are_not_duplicated(self):
        mesh = MeshData(
            vertices=((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            faces=((0, 1, 2),),
        )

        mirrored = mirror_symmetry(mesh, ("X",))

        self.assertEqual(len(mirrored.faces), 1)
        self.assertEqual(len({tuple(sorted(face)) for face in mirrored.faces}), len(mirrored.faces))

    def test_mirror_multi_axis_builds_exact_vertex_symmetry(self):
        mesh = MeshData(
            vertices=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 3.0)),
            faces=((0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)),
            hard_edges=frozenset({(1, 2)}),
        )

        mirrored = mirror_symmetry(mesh, ("Z", "X"))

        self.assertEqual(symmetry_error(mirrored, ("X", "Z")), 0.0)
        self.assertEqual(len({tuple(sorted(face)) for face in mirrored.faces}), len(mirrored.faces))
        self.assertGreater(len(mirrored.hard_edges), 1)

    def test_clipped_then_mirrored_cube_is_watertight_and_area_preserved(self):
        cube = _triangulated_cube()
        source_area = _surface_area(cube)
        clipped, density = clip_to_symmetry(cube, tuple(float(index) for index in range(len(cube.vertices))), ("X",))
        mirrored = mirror_symmetry(clipped, ("X",))
        analysis = analyze_mesh(mirrored)

        self.assertEqual(len(density), len(clipped.vertices))
        self.assertIn(0.5, density)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertAlmostEqual(_surface_area(mirrored), source_area, places=7)
        self.assertEqual(symmetry_error(mirrored, ("X",)), 0.0)
        _validate_topology(mirrored, 45.0)

    def test_multi_axis_clipped_cube_has_valid_winding_and_no_bowtie(self):
        cube = _triangulated_cube()
        clipped, _ = clip_to_symmetry(cube, (), ("X", "Y"))
        mirrored = mirror_symmetry(clipped, ("X", "Y"))
        analysis = analyze_mesh(mirrored)

        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.boundary_edge_count, 0)
        _validate_topology(mirrored, 45.0)

    def test_raw_float_ico_sphere_xyz_clip_triangulates_and_mirrors_closed(self):
        sphere = _ico_sphere_fixture()

        clipped, _ = clip_to_symmetry(sphere, (), ("X", "Y", "Z"))
        _validate_topology(clipped, 180.0)
        mirrored = mirror_symmetry(clipped, ("X", "Y", "Z"))
        analysis = analyze_mesh(mirrored)

        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(analysis.degenerate_face_count, 0)
        _validate_topology(mirrored, 180.0)

    def test_mirror_axes_order_is_canonical(self):
        mesh = MeshData(
            vertices=((1.0, 2.0, 0.0), (2.0, 2.0, 0.0), (1.0, 3.0, 0.0)),
            faces=((0, 1, 2),),
        )

        self.assertEqual(mirror_symmetry(mesh, ("Y", "X")), mirror_symmetry(mesh, ("X", "Y")))

    def test_mirror_preserves_separate_components_with_same_coordinates(self):
        mesh = MeshData(
            vertices=(
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2), (3, 4, 5)),
        )

        mirrored = mirror_symmetry(mesh, ("X",))

        self.assertEqual(len(mirrored.vertices), 8)
        self.assertEqual(len(mirrored.faces), 4)
        self.assertEqual(analyze_mesh(mirrored).non_manifold_edge_count, 0)

    def test_clip_hard_edges_do_not_absorb_collinear_other_component_vertices(self):
        mesh = MeshData(
            vertices=(
                (-1.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (1.0, 1.0, 0.0),
                (0.5, 0.0, 0.0),
                (0.5, 2.0, 0.0),
                (1.5, 2.0, 0.0),
            ),
            faces=((0, 1, 2), (3, 4, 5)),
            hard_edges=frozenset({(0, 1)}),
        )

        clipped, _ = clip_to_symmetry(mesh, (), ("X",))
        hard_edge_points = [_edge_points(clipped, edge) for edge in clipped.hard_edges]

        self.assertIn({(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)}, hard_edge_points)
        self.assertFalse(any((0.5, 0.0, 0.0) in points for points in hard_edge_points))

    def test_invalid_axis_is_rejected(self):
        mesh = MeshData(
            vertices=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "지원하지 않는"):
            clip_to_symmetry(mesh, (), ("W",))
        with self.assertRaisesRegex(ValueError, "지원하지 않는"):
            mirror_symmetry(mesh, ("W",))


def _triangulated_cube() -> MeshData:
    vertices = (
        (-1.0, -1.0, -1.0),
        (1.0, -1.0, -1.0),
        (1.0, 1.0, -1.0),
        (-1.0, 1.0, -1.0),
        (-1.0, -1.0, 1.0),
        (1.0, -1.0, 1.0),
        (1.0, 1.0, 1.0),
        (-1.0, 1.0, 1.0),
    )
    faces = (
        (0, 3, 2),
        (0, 2, 1),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    )
    return MeshData(vertices=vertices, faces=faces)


def _ico_sphere_fixture() -> MeshData:
    vertices = [
        (-0.525731086730957, 0.8506507873535156, 0.0),
        (0.525731086730957, 0.8506507873535156, 0.0),
        (-0.525731086730957, -0.8506507873535156, 0.0),
        (0.525731086730957, -0.8506507873535156, 0.0),
        (0.0, -0.525731086730957, 0.8506507873535156),
        (0.0, 0.525731086730957, 0.8506507873535156),
        (0.0, -0.525731086730957, -0.8506507873535156),
        (0.0, 0.525731086730957, -0.8506507873535156),
        (0.8506507873535156, 0.0, -0.525731086730957),
        (0.8506507873535156, 0.0, 0.525731086730957),
        (-0.8506507873535156, 0.0, -0.525731086730957),
        (-0.8506507873535156, 0.0, 0.525731086730957),
        (-0.80901700258255, 0.5, 0.30901700258255005),
        (-0.5, 0.30901700258255005, 0.80901700258255),
        (-0.30901700258255005, 0.80901700258255, 0.5),
        (0.30901700258255005, 0.80901700258255, 0.5),
        (0.0, 1.0, 0.0),
        (0.30901700258255005, 0.80901700258255, -0.5),
        (-0.30901700258255005, 0.80901700258255, -0.5),
        (-0.5, 0.30901700258255005, -0.80901700258255),
        (-0.80901700258255, 0.5, -0.30901700258255005),
        (-1.0, 0.0, 0.0),
        (-0.80901700258255, -0.5, 0.30901700258255005),
        (-0.5, -0.30901700258255005, 0.80901700258255),
        (0.0, 0.0, 1.0),
        (0.5, 0.30901700258255005, 0.80901700258255),
        (0.80901700258255, 0.5, 0.30901700258255005),
        (0.5, 0.30901700258255005, -0.80901700258255),
        (0.80901700258255, 0.5, -0.30901700258255005),
        (1.0, 0.0, 0.0),
        (0.80901700258255, -0.5, 0.30901700258255005),
        (0.5, -0.30901700258255005, 0.80901700258255),
        (-0.30901700258255005, -0.80901700258255, 0.5),
        (0.30901700258255005, -0.80901700258255, 0.5),
        (0.0, -1.0, 0.0),
        (0.30901700258255005, -0.80901700258255, -0.5),
        (-0.30901700258255005, -0.80901700258255, -0.5),
        (-0.5, -0.30901700258255005, -0.80901700258255),
        (-0.80901700258255, -0.5, -0.30901700258255005),
        (0.5, -0.30901700258255005, -0.80901700258255),
        (0.80901700258255, -0.5, -0.30901700258255005),
        (0.0, 0.0, -1.0),
    ]
    faces = [
        (0, 12, 14), (0, 14, 16), (0, 16, 18), (0, 18, 20), (0, 20, 12),
        (1, 15, 26), (1, 26, 28), (1, 28, 17), (1, 17, 16), (1, 16, 15),
        (2, 32, 22), (2, 22, 38), (2, 38, 36), (2, 36, 34), (2, 34, 32),
        (3, 30, 33), (3, 33, 34), (3, 34, 35), (3, 35, 40), (3, 40, 30),
        (4, 23, 24), (4, 24, 31), (4, 31, 33), (4, 33, 32), (4, 32, 23),
        (5, 13, 25), (5, 25, 15), (5, 15, 16), (5, 16, 14), (5, 14, 13),
        (6, 37, 39), (6, 39, 40), (6, 40, 35), (6, 35, 36), (6, 36, 37),
        (7, 18, 17), (7, 17, 28), (7, 28, 27), (7, 27, 19), (7, 19, 18),
        (8, 27, 28), (8, 28, 29), (8, 29, 40), (8, 40, 39), (8, 39, 27),
        (9, 26, 25), (9, 25, 31), (9, 31, 30), (9, 30, 29), (9, 29, 26),
        (10, 20, 19), (10, 19, 37), (10, 37, 38), (10, 38, 21), (10, 21, 20),
        (11, 12, 21), (11, 21, 22), (11, 22, 23), (11, 23, 13), (11, 13, 12),
        (12, 20, 21), (13, 23, 24), (14, 12, 13), (15, 25, 26), (17, 18, 16),
        (19, 20, 18), (22, 21, 38), (24, 23, 32), (25, 24, 31), (27, 37, 19),
        (29, 28, 26), (30, 31, 33), (35, 34, 36), (37, 36, 38), (39, 40, 35),
    ]
    return MeshData(tuple(vertices), tuple(faces))


def _edge_points(mesh: MeshData, edge: tuple[int, int]) -> set[tuple[float, float, float]]:
    return {mesh.vertices[edge[0]], mesh.vertices[edge[1]]}


def _ico_sphere_fixture() -> MeshData:
    t = (1.0 + 5.0 ** 0.5) * 0.5
    vertices = [
        (-1.0, t, 0.0), (1.0, t, 0.0), (-1.0, -t, 0.0), (1.0, -t, 0.0),
        (0.0, -1.0, t), (0.0, 1.0, t), (0.0, -1.0, -t), (0.0, 1.0, -t),
        (t, 0.0, -1.0), (t, 0.0, 1.0), (-t, 0.0, -1.0), (-t, 0.0, 1.0),
    ]
    vertices = [_normalize_raw_float(vertex) for vertex in vertices]
    faces = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]
    for _ in range(2):
        midpoint_cache = {}
        next_faces = []
        for a, b, c in faces:
            ab = _midpoint_index(vertices, midpoint_cache, a, b)
            bc = _midpoint_index(vertices, midpoint_cache, b, c)
            ca = _midpoint_index(vertices, midpoint_cache, c, a)
            next_faces.extend(((a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)))
        faces = next_faces
    vertices = tuple(_near_axis_raw_float(vertex) for vertex in vertices)
    return MeshData(vertices, tuple(faces))


def _midpoint_index(vertices, cache, first, second):
    key = tuple(sorted((first, second)))
    if key not in cache:
        midpoint = tuple((vertices[first][axis] + vertices[second][axis]) * 0.5 for axis in range(3))
        vertices.append(_normalize_raw_float(midpoint))
        cache[key] = len(vertices) - 1
    return cache[key]


def _normalize_raw_float(vertex):
    length = math.sqrt(sum(component * component for component in vertex))
    return tuple(float(component / length) for component in vertex)


def _near_axis_raw_float(vertex):
    values = []
    for index, component in enumerate(vertex):
        if abs(component) == 0.0:
            values.append((1.0 if index % 2 == 0 else -1.0) * 1.2e-8)
        else:
            values.append(float(component))
    return tuple(values)


def _surface_area(mesh: MeshData) -> float:
    area = 0.0
    for face in mesh.faces:
        origin = mesh.vertices[face[0]]
        for index in range(1, len(face) - 1):
            area += _triangle_area(origin, mesh.vertices[face[index]], mesh.vertices[face[index + 1]])
    return area


def _triangle_area(a, b, c) -> float:
    ab = tuple(b[index] - a[index] for index in range(3))
    ac = tuple(c[index] - a[index] for index in range(3))
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return 0.5 * sum(component * component for component in cross) ** 0.5


if __name__ == "__main__":
    unittest.main()
