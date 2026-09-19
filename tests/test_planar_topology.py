from __future__ import annotations

import unittest
from collections import Counter, defaultdict

from addon.core import GuideCurveData, MeshData, RemeshCancelled, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.planar import try_remesh_planar


class PlanarTopologyTests(unittest.TestCase):
    def test_cube_target_384_builds_six_8_by_8_charts(self):
        result = try_remesh_planar(build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384)))

        self.assertIsNotNone(result)
        assert result is not None
        analysis = analyze_mesh(result)
        self.assertEqual(analysis.quad_count, 384)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(len(result.hard_edges), 96)
        self.assertEqual(_axis_values(result, 0), 9)
        self.assertEqual(_axis_values(result, 1), 9)
        self.assertEqual(_axis_values(result, 2), 9)

    def test_cube_has_no_interior_planar_poles(self):
        result = try_remesh_planar(build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384)))

        self.assertIsNotNone(result)
        assert result is not None
        crease_vertices = {vertex for edge in result.hard_edges for vertex in edge}
        valence = _vertex_valence(result)
        interior_vertices = set(range(len(result.vertices))) - crease_vertices
        self.assertTrue(interior_vertices)
        self.assertEqual({valence[index] for index in interior_vertices}, {4})

    def test_triangulated_cube_still_extracts_six_planar_charts(self):
        result = try_remesh_planar(build_engine_input(_triangulated_cube_mesh(), RemeshSettings(target_quad_count=384)))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 384)
        self.assertEqual(len(result.hard_edges), 96)

    def test_box_uses_shared_counts_on_parallel_dimensions(self):
        result = try_remesh_planar(build_engine_input(_box_mesh(), RemeshSettings(target_quad_count=160)))

        self.assertIsNotNone(result)
        assert result is not None
        analysis = analyze_mesh(result)
        self.assertEqual(analysis.quad_count, 160)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(_axis_values(result, 0), 9)
        self.assertEqual(_axis_values(result, 1), 5)
        self.assertEqual(_axis_values(result, 2), 5)

    def test_uniform_default_density_is_supported(self):
        mesh = _cube_mesh()
        result = try_remesh_planar(
            build_engine_input(mesh, RemeshSettings(target_quad_count=384), density_values=(1.0,) * len(mesh.vertices))
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 384)

    def test_dense_cube_output_removes_unused_source_vertices(self):
        mesh = _subdivided_cube_mesh(43)
        self.assertEqual(len(mesh.faces), 11094)

        result = try_remesh_planar(build_engine_input(mesh, RemeshSettings(target_quad_count=384)))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 384)
        self.assertEqual(_loose_vertex_count(result), 0)

    def test_non_uniform_density_returns_none(self):
        mesh = _cube_mesh()
        density_values = (1.0, 1.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0)

        self.assertIsNone(try_remesh_planar(build_engine_input(mesh, RemeshSettings(target_quad_count=384), density_values=density_values)))

    def test_guide_curve_returns_none_instead_of_ignoring_constraint(self):
        guide = GuideCurveData("REMESH_GUIDE_main", (((-1, 0, 0), (1, 0, 0)),))
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384), guide_curves=(guide,))

        self.assertIsNone(try_remesh_planar(engine_input))

    def test_guide_curve_names_return_none_even_when_curves_are_missing(self):
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384, guide_curve_names=("REMESH_GUIDE_missing",)))

        self.assertIsNone(try_remesh_planar(engine_input))

    def test_symmetric_cube_accepts_symmetry_axes(self):
        result = try_remesh_planar(build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384, symmetry_axes=("X", "Y", "Z"))))

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 384)
        self.assertEqual(_symmetry_error(result, "X"), 0.0)
        self.assertEqual(_symmetry_error(result, "Y"), 0.0)
        self.assertEqual(_symmetry_error(result, "Z"), 0.0)

    def test_inward_acceptance_cube_accepts_symmetry_target_24(self):
        result = try_remesh_planar(build_engine_input(_inward_acceptance_cube_mesh(), RemeshSettings(target_quad_count=24, symmetry_axes=("X", "Y", "Z"))))

        self.assertIsNotNone(result)
        assert result is not None
        analysis = analyze_mesh(result)
        self.assertEqual(analysis.quad_count, 24)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(_symmetry_error(result, "X"), 0.0)
        self.assertEqual(_symmetry_error(result, "Y"), 0.0)
        self.assertEqual(_symmetry_error(result, "Z"), 0.0)

    def test_asymmetric_mesh_rejects_symmetry_axes(self):
        self.assertIsNone(try_remesh_planar(build_engine_input(_asymmetric_box_mesh(), RemeshSettings(target_quad_count=160, symmetry_axes=("X",)))))

    def test_open_plane_is_unsupported(self):
        mesh = MeshData(vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), faces=((0, 1, 2, 3),))

        self.assertIsNone(try_remesh_planar(build_engine_input(mesh, RemeshSettings(target_quad_count=16))))

    def test_cancellation_is_observed(self):
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384))

        with self.assertRaises(RemeshCancelled):
            try_remesh_planar(engine_input, cancelled=lambda: True)


def _cube_mesh() -> MeshData:
    return MeshData(
        vertices=(
            (-1, -1, -1),
            (1, -1, -1),
            (1, 1, -1),
            (-1, 1, -1),
            (-1, -1, 1),
            (1, -1, 1),
            (1, 1, 1),
            (-1, 1, 1),
        ),
        faces=((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)),
    )


def _triangulated_cube_mesh() -> MeshData:
    faces = []
    for a, b, c, d in _cube_mesh().faces:
        faces.append((a, b, c))
        faces.append((a, c, d))
    return MeshData(vertices=_cube_mesh().vertices, faces=tuple(faces))


def _subdivided_cube_mesh(segments: int) -> MeshData:
    base = _cube_mesh()
    vertices: list[tuple[float, float, float]] = []
    vertex_indices: dict[tuple[float, float, float], int] = {}
    faces: list[tuple[int, int, int, int]] = []

    def vertex_index(point: tuple[float, float, float]) -> int:
        key = tuple(round(component, 10) for component in point)
        if key not in vertex_indices:
            vertex_indices[key] = len(vertices)
            vertices.append(point)
        return vertex_indices[key]

    for face in base.faces:
        corners = tuple(base.vertices[index] for index in face)
        for y in range(segments):
            for x in range(segments):
                u0 = x / segments
                u1 = (x + 1) / segments
                v0 = y / segments
                v1 = (y + 1) / segments
                faces.append(
                    (
                        vertex_index(_quad_point(corners, u0, v0)),
                        vertex_index(_quad_point(corners, u1, v0)),
                        vertex_index(_quad_point(corners, u1, v1)),
                        vertex_index(_quad_point(corners, u0, v1)),
                    )
                )
    return MeshData(tuple(vertices), tuple(faces))


def _quad_point(corners, u: float, v: float) -> tuple[float, float, float]:
    top = tuple(corners[0][axis] + (corners[1][axis] - corners[0][axis]) * u for axis in range(3))
    bottom = tuple(corners[3][axis] + (corners[2][axis] - corners[3][axis]) * u for axis in range(3))
    return tuple(top[axis] + (bottom[axis] - top[axis]) * v for axis in range(3))


def _box_mesh() -> MeshData:
    return MeshData(
        vertices=(
            (-2, -1, -1),
            (2, -1, -1),
            (2, 1, -1),
            (-2, 1, -1),
            (-2, -1, 1),
            (2, -1, 1),
            (2, 1, 1),
            (-2, 1, 1),
        ),
        faces=((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)),
    )


def _inward_acceptance_cube_mesh() -> MeshData:
    return MeshData(
        vertices=(
            (-1, -1, -1),
            (1, -1, -1),
            (1, 1, -1),
            (-1, 1, -1),
            (-1, -1, 1),
            (1, -1, 1),
            (1, 1, 1),
            (-1, 1, 1),
        ),
        faces=((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)),
    )


def _asymmetric_box_mesh() -> MeshData:
    return MeshData(
        vertices=(
            (0, -1, -1),
            (4, -1, -1),
            (4, 1, -1),
            (0, 1, -1),
            (0, -1, 1),
            (4, -1, 1),
            (4, 1, 1),
            (0, 1, 1),
        ),
        faces=((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)),
    )


def _axis_values(mesh: MeshData, axis: int) -> int:
    return len({round(vertex[axis], 8) for vertex in mesh.vertices})


def _vertex_valence(mesh: MeshData) -> Counter[int]:
    neighbors: dict[int, set[int]] = defaultdict(set)
    for face in mesh.faces:
        for first, second in zip(face, (*face[1:], face[0])):
            neighbors[first].add(second)
            neighbors[second].add(first)
    return Counter({index: len(neighbors[index]) for index in range(len(mesh.vertices))})


def _loose_vertex_count(mesh: MeshData) -> int:
    used = {vertex for face in mesh.faces for vertex in face}
    return len(set(range(len(mesh.vertices))) - used)


def _symmetry_error(mesh: MeshData, axis: str) -> float:
    axis_index = "XYZ".index(axis)
    points = {tuple(round(component, 8) for component in vertex) for vertex in mesh.vertices}
    maximum = 0.0
    for vertex in mesh.vertices:
        mirrored = tuple(-component if index == axis_index else component for index, component in enumerate(vertex))
        if tuple(round(component, 8) for component in mirrored) not in points:
            maximum = max(maximum, min(sum((mirrored[index] - other[index]) ** 2 for index in range(3)) ** 0.5 for other in mesh.vertices))
    return maximum


if __name__ == "__main__":
    unittest.main()
