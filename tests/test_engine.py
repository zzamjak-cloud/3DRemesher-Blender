from __future__ import annotations

import unittest
from unittest.mock import patch

from addon.core import (
    GuideCurveData,
    MeshData,
    RemeshBackend,
    RemeshCancelled,
    RemeshSettings,
    analyze_mesh,
    build_engine_input,
)
from addon.engine import MAX_FACE_VERTICES, MAX_OUTPUT_QUADS


class EngineTests(unittest.TestCase):
    def test_closed_cube_outputs_all_quads_without_non_manifold_edges(self):
        mesh = _cube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=24))

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.actual_quad_count, 24)
        self.assertEqual(result.quality.quad_ratio, 1.0)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)
        self.assertEqual(result.quality.boundary_edge_count, 0)
        for vertex in mesh.vertices:
            self.assertIn(vertex, result.mesh.vertices)

    def test_direct_quad_candidate_can_meet_cube_six_target(self):
        result=RemeshBackend().remesh(build_engine_input(_cube_mesh(),RemeshSettings(target_quad_count=6)))
        self.assertEqual(result.quality.actual_quad_count,6)
        self.assertEqual(result.quality.boundary_edge_count,0)

    def test_symmetry_budget_compares_neighboring_patch_counts(self):
        result=RemeshBackend().remesh(build_engine_input(_cube_mesh(),RemeshSettings(target_quad_count=64,symmetry_axes=("X","Y","Z"))))
        self.assertLessEqual(result.quality.target_error_ratio,.125)
        self.assertEqual(result.quality.symmetry_error,0.)
        self.assertEqual(result.quality.boundary_edge_count,0)

    def test_triangulated_planar_patch_pairs_to_quad_patch(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2), (0, 2, 3)),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=4))

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.actual_quad_count, 4)
        self.assertEqual(result.quality.boundary_edge_count, 8)
        self.assertTrue(all(len(face) == 4 for face in result.mesh.faces))

    def test_concave_ngon_uses_safe_ear_clipping(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 0.35, 0), (0, 1, 0)),
            faces=((0, 1, 2, 3, 4),),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=12))

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.quad_ratio, 1.0)
        self.assertGreaterEqual(result.quality.actual_quad_count, 9)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)

    def test_concave_ngon_preserves_clockwise_winding(self):
        vertices = ((0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 0.35, 0), (0, 1, 0))
        clockwise = MeshData(vertices=vertices, faces=((4, 3, 2, 1, 0),))
        engine_input = build_engine_input(clockwise, RemeshSettings(target_quad_count=12))

        result = RemeshBackend().remesh(engine_input)
        signed_areas = [_signed_xy_area(result.mesh.vertices, face) for face in result.mesh.faces]

        self.assertLessEqual(abs(result.quality.actual_quad_count-12), 3)
        self.assertLess(max(signed_areas), 0.0)

    def test_feature_edge_blocks_triangle_pair_and_preserves_split_chain(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2), (0, 2, 3)),
            hard_edges=frozenset({(0, 2)}),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=6))

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.actual_quad_count, 6)
        self.assertGreaterEqual(len(result.mesh.hard_edges), 2)
        hard_edge_vertices = {vertex for edge in result.mesh.hard_edges for vertex in edge}
        self.assertIn(0, hard_edge_vertices)
        self.assertIn(2, hard_edge_vertices)

    def test_symmetry_and_density_controls_are_applied(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )
        engine_input = build_engine_input(
            mesh,
            RemeshSettings(target_quad_count=4, symmetry_axes=("X",), density_scale=2.0),
            density_values=(1.0, 1.0, 1.0),
        )

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.unsupported_controls, ())
        self.assertEqual(result.quality.symmetry_error, 0.0)
        self.assertTrue(any(v[0]<0 for v in result.mesh.vertices))

    def test_target_bounds_are_enforced(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=400000))

        with patch("addon.engine.MAX_OUTPUT_QUADS", 64):
            result = RemeshBackend().remesh(engine_input)

        self.assertLessEqual(result.quality.actual_quad_count, 64)
        self.assertTrue(any("target=400000" in warning for warning in result.warnings))

    def test_rejects_oversized_ngon_before_triangulation(self):
        vertices = tuple((float(index), 0.0, 0.0) for index in range(MAX_FACE_VERTICES + 1))
        mesh = MeshData(vertices=vertices, faces=(tuple(range(MAX_FACE_VERTICES + 1)),))
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=4))

        with self.assertRaisesRegex(ValueError, "정점 수가 너무 많습니다"):
            RemeshBackend().remesh(engine_input)

    def test_deterministic_output(self):
        mesh = _cube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=96))

        first = RemeshBackend().remesh(engine_input)
        second = RemeshBackend().remesh(engine_input)

        self.assertEqual(first.mesh, second.mesh)
        self.assertEqual(first.quality, second.quality)
        self.assertEqual(first.warnings, second.warnings)

    def test_rejects_duplicate_faces_without_mutating_input(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2), (2, 1, 0)),
        )
        original_vertices = mesh.vertices
        original_faces = mesh.faces
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=4))

        with self.assertRaisesRegex(ValueError, "중복 면"):
            RemeshBackend().remesh(engine_input)

        self.assertEqual(mesh.vertices, original_vertices)
        self.assertEqual(mesh.faces, original_faces)

    def test_rejects_wrong_winding_and_self_intersection(self):
        bad_winding = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2), (0, 3, 2)),
        )
        with self.assertRaisesRegex(ValueError, "winding"):
            RemeshBackend().remesh(build_engine_input(bad_winding, RemeshSettings(target_quad_count=4)))

        bow_tie = MeshData(
            vertices=((0, 0, 0), (1, 1, 0), (0, 1, 0), (1, 0, 0)),
            faces=((0, 1, 2, 3),),
        )
        with self.assertRaisesRegex(ValueError, "자기교차"):
            RemeshBackend().remesh(build_engine_input(bow_tie, RemeshSettings(target_quad_count=4)))

    def test_cancellation_keeps_topology_unchanged(self):
        mesh = _cube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=96))
        progress_events: list[tuple[float, str]] = []

        def progress(fraction: float, message: str) -> None:
            progress_events.append((fraction, message))

        with self.assertRaises(RemeshCancelled):
            RemeshBackend().remesh(engine_input, progress=progress, cancelled=lambda: bool(progress_events))

        self.assertEqual(analyze_mesh(mesh).quad_count, 6)

    def test_guide_curve_can_change_triangle_pair_selection(self):
        height = 0.8660254037844386
        mesh = MeshData(
            vertices=(
                (0, 0, 0),
                (1, 0, 0),
                (0.5, height, 0),
                (1.5, height, 0),
                (1, 2 * height, 0),
            ),
            faces=((0, 1, 2), (1, 3, 2), (2, 3, 4)),
        )
        guide = GuideCurveData(name="REMESH_GUIDE_low", splines=(((0, 0, 0), (1.5, 0, 0)),))

        without_guide = RemeshBackend().remesh(build_engine_input(mesh, RemeshSettings(target_quad_count=7)))
        with_guide = RemeshBackend().remesh(
            build_engine_input(mesh, RemeshSettings(target_quad_count=7), guide_curves=(guide,))
        )

        self.assertEqual(without_guide.quality.actual_quad_count, 7)
        self.assertEqual(with_guide.quality.actual_quad_count, 7)
        self.assertNotEqual(without_guide.mesh.faces, with_guide.mesh.faces)
        self.assertEqual(with_guide.quality.quad_ratio, 1.0)


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
        faces=(
            (0, 3, 2, 1),
            (4, 5, 6, 7),
            (0, 1, 5, 4),
            (1, 2, 6, 5),
            (2, 3, 7, 6),
            (3, 0, 4, 7),
        ),
    )


def _signed_xy_area(vertices, face) -> float:
    area = 0.0
    for first, second in zip(face, (*face[1:], face[0])):
        first_vertex = vertices[first]
        second_vertex = vertices[second]
        area += first_vertex[0] * second_vertex[1] - second_vertex[0] * first_vertex[1]
    return area


if __name__ == "__main__":
    unittest.main()
