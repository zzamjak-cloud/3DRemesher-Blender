from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, analyze_mesh, build_engine_input


class CoreTests(unittest.TestCase):
    def test_analyze_quad_plane(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2, 3),),
        )

        analysis = analyze_mesh(mesh)

        self.assertEqual(analysis.vertex_count, 4)
        self.assertEqual(analysis.edge_count, 4)
        self.assertEqual(analysis.quad_count, 1)
        self.assertEqual(analysis.quad_ratio, 1.0)
        self.assertEqual(analysis.boundary_edge_count, 4)

    def test_detect_degenerate_face(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 0, 0)),
            faces=((0, 1, 2),),
        )

        self.assertEqual(analyze_mesh(mesh).degenerate_face_count, 1)

    def test_build_engine_input_validates_density_count(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "밀도 값 개수"):
            build_engine_input(mesh, RemeshSettings(), density_values=(1.0, 0.5))

    def test_rejects_non_finite_vertex_coordinates(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (math.nan, 1, 0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "유한한 숫자"):
            analyze_mesh(mesh)

    def test_rejects_invalid_hard_edge_indices(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
            hard_edges=frozenset({(0, 9)}),
        )

        with self.assertRaisesRegex(ValueError, "하드 엣지"):
            analyze_mesh(mesh)

    def test_rejects_empty_density_name(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "밀도 속성 이름"):
            build_engine_input(mesh, RemeshSettings(density_attribute_name=" "))

    def test_rejects_non_finite_density_and_guide_values(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )

        with self.assertRaisesRegex(ValueError, "밀도 값"):
            build_engine_input(mesh, RemeshSettings(), density_values=(1.0, math.inf, 0.5))

        guide = GuideCurveData(name="REMESH_GUIDE_bad", splines=(((0.0, 0.0, math.nan),),))
        with self.assertRaisesRegex(ValueError, "좌표"):
            build_engine_input(mesh, RemeshSettings(), guide_curves=(guide,))

    def test_backend_marks_remesh_unimplemented(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0)),
            faces=((0, 1, 2),),
        )
        engine_input = RemeshBackend().build_input(mesh, RemeshSettings(target_quad_count=8))

        with self.assertRaisesRegex(NotImplementedError, "아직 구현"):
            RemeshBackend().remesh(engine_input)


if __name__ == "__main__":
    unittest.main()
