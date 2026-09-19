"""새 얼굴 패치의 실패 처리와 기본 위상 회귀 검사."""

from __future__ import annotations

import unittest

from addon.core import GuideCurveData, MeshData, RemeshCancelled, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.face_patch import _signed_volume, try_remesh_face_patch
from tests.test_face_patch_integration import _face_guides, _sphere_mesh


class FacePatchTests(unittest.TestCase):
    def setUp(self):
        self.source = _sphere_mesh(31, 17, alternating=False)
        self.settings = RemeshSettings(target_quad_count=2400, topology_mode="STRUCTURED")
        self.guides = _face_guides()

    def test_guides_create_closed_quad_surface(self):
        result = try_remesh_face_patch(build_engine_input(self.source, self.settings, self.guides))
        self.assertIsNotNone(result)
        assert result is not None
        analysis = analyze_mesh(result)
        self.assertEqual(analysis.quad_ratio, 1.0)
        self.assertEqual(analysis.boundary_edge_count, 0)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertNotEqual(result.vertices, self.source.vertices)

    def test_cancellation_propagates(self):
        with self.assertRaises(RemeshCancelled):
            try_remesh_face_patch(
                build_engine_input(self.source, self.settings, self.guides),
                cancelled=lambda: True,
            )

    def test_overlapping_guides_are_rejected(self):
        duplicated = GuideCurveData(
            "duplicated",
            self.guides[0].splines,
            kind=("LOOP",),
            closed=(True,),
        )
        result = try_remesh_face_patch(build_engine_input(self.source, self.settings, self.guides + (duplicated,)))
        self.assertIsNone(result)

    def test_open_source_is_rejected(self):
        source = MeshData(self.source.vertices, self.source.faces[:-1])
        result = try_remesh_face_patch(build_engine_input(source, self.settings, self.guides))
        self.assertIsNone(result)

    def test_symmetry_requires_a_different_path(self):
        settings = RemeshSettings(target_quad_count=2400, symmetry_axes=("X",), topology_mode="STRUCTURED")
        result = try_remesh_face_patch(build_engine_input(self.source, settings, self.guides))
        self.assertIsNone(result)

    def test_source_winding_is_preserved(self):
        for source in (
            self.source,
            MeshData(self.source.vertices, tuple(tuple(reversed(face)) for face in self.source.faces)),
        ):
            with self.subTest(source_volume=_signed_volume(source)):
                result = try_remesh_face_patch(build_engine_input(source, self.settings, self.guides))
                self.assertIsNotNone(result)
                assert result is not None
                self.assertGreater(_signed_volume(source) * _signed_volume(result), 0.0)


if __name__ == "__main__":
    unittest.main()
