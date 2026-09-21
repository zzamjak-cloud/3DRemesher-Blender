"""QuadriFlow 범용 경로의 모드·라우팅 계약을 bpy 없는 환경에서 검증한다."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from addon.core import (
    GuideCurveData,
    MeshData,
    RemeshBackend,
    RemeshResult,
    RemeshSettings,
    build_engine_input,
)
from addon.engine import try_quadriflow_remesh
from addon.quadriflow_path import is_available, unsupported_reason


class QuadriflowPathTests(unittest.TestCase):
    def test_quadriflow_mode_is_accepted_by_settings(self):
        self.assertEqual(RemeshSettings(topology_mode="quadriflow").topology_mode, "QUADRIFLOW")
        build_engine_input(_cube_mesh(), RemeshSettings(topology_mode="QUADRIFLOW"))

    def test_not_available_without_bpy(self):
        self.assertFalse(is_available())

    def test_required_guides_are_unsupported(self):
        loop = GuideCurveData(
            name="REMESH_GUIDE_ring",
            splines=(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),),
            kind=("LOOP",),
        )
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(topology_mode="AUTO"), (loop,))

        self.assertIn("LOOP/STRIP", unsupported_reason(engine_input))
        self.assertEqual(unsupported_reason(build_engine_input(_cube_mesh(), RemeshSettings())), "")

    def test_auto_skips_quadriflow_without_bpy(self):
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(topology_mode="AUTO"))

        result, reason = try_quadriflow_remesh(engine_input, progress=None, cancelled=None)

        self.assertIsNone(result)
        self.assertIn("Blender Python", reason)

    def test_quadriflow_mode_requires_bpy(self):
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(topology_mode="QUADRIFLOW"))

        with self.assertRaisesRegex(RuntimeError, "Blender Python"):
            RemeshBackend().remesh(engine_input)

    def test_quadriflow_mode_rejects_required_guides_before_running(self):
        loop = GuideCurveData(
            name="REMESH_GUIDE_ring",
            splines=(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),),
            kind=("LOOP",),
        )
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(topology_mode="QUADRIFLOW"), (loop,))

        with self.assertRaisesRegex(ValueError, "가이드"):
            RemeshBackend().remesh(engine_input)

    def test_auto_skips_quadriflow_for_density_and_direction_controls(self):
        mesh = _irregular_mesh()
        direction = GuideCurveData(name="REMESH_GUIDE_dir", splines=(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),))
        density = tuple(float(index % 2 + 1) for index in range(len(mesh.vertices)))
        engine_input = build_engine_input(mesh, RemeshSettings(topology_mode="AUTO"), (direction,), density)

        with patch("addon.quadriflow_path.is_available", return_value=True), patch(
            "addon.quadriflow_path.remesh_quadriflow", side_effect=AssertionError("미지원 제어가 있으면 호출되지 않아야 합니다.")
        ):
            result, reason = try_quadriflow_remesh(engine_input, progress=None, cancelled=None)

        self.assertIsNone(result)
        self.assertIn("밀도 속성", reason)
        self.assertIn("DIRECTION 가이드", reason)

    def test_auto_prefers_structured_grid_over_quadriflow(self):
        engine_input = build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=24, topology_mode="AUTO"))

        with patch("addon.engine.try_quadriflow_remesh", side_effect=AssertionError("격자 성공 뒤에는 호출되지 않아야 합니다.")):
            result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.actual_quad_count, 24)

    def test_auto_uses_quadriflow_result_when_grid_fails(self):
        mesh = _irregular_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=12, topology_mode="AUTO"))
        fake = RemeshResult(mesh, RemeshBackend().remesh(engine_input).quality, ("가짜 QuadriFlow",), ())

        with patch("addon.engine.try_quadriflow_remesh", return_value=(fake, "")):
            result = RemeshBackend().remesh(engine_input)

        self.assertIs(result, fake)

    def test_auto_reports_quadriflow_skip_reason_in_legacy_warnings(self):
        engine_input = build_engine_input(_irregular_mesh(), RemeshSettings(target_quad_count=12, topology_mode="AUTO"))

        result = RemeshBackend().remesh(engine_input)

        self.assertTrue(any("실험 엔진" in warning for warning in result.warnings))
        self.assertTrue(any("QuadriFlow" in warning for warning in result.warnings))

    def test_quadriflow_mode_propagates_path_failure(self):
        engine_input = build_engine_input(_irregular_mesh(), RemeshSettings(target_quad_count=12, topology_mode="QUADRIFLOW"))

        with patch("addon.quadriflow_path.is_available", return_value=True), patch(
            "addon.quadriflow_path.remesh_quadriflow", side_effect=ValueError("의도한 실패")
        ):
            with self.assertRaisesRegex(ValueError, "의도한 실패"):
                RemeshBackend().remesh(engine_input)


def _cube_mesh() -> MeshData:
    vertices = (
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
    )
    faces = (
        (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
        (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
    )
    return MeshData(vertices, faces)


def _irregular_mesh() -> MeshData:
    """격자 전략이 다루지 못하는 비평면 삼각 표면 조각."""
    vertices = (
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.1), (2.0, 0.0, 0.0),
        (0.0, 1.0, 0.2), (1.0, 1.0, 0.5), (2.0, 1.0, 0.1),
        (0.0, 2.0, 0.0), (1.0, 2.0, 0.15), (2.0, 2.0, 0.0),
    )
    faces = (
        (0, 1, 4), (0, 4, 3), (1, 2, 5), (1, 5, 4),
        (3, 4, 7), (3, 7, 6), (4, 5, 8), (4, 8, 7),
    )
    return MeshData(vertices, faces)


if __name__ == "__main__":
    unittest.main()
