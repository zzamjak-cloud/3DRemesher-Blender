"""삼각 연결을 바꿔도 새 둘레 링을 만드는지 확인한다."""

from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout
from addon.topology.triangulated_limb import try_remesh_triangulated_limb


def _tube(segments: int, intervals: int, *, alternating: bool) -> MeshData:
    vertices = []
    for row in range(intervals + 1):
        z = 3.0 * row / intervals
        cx = 0.15 * math.sin(math.pi * z / 3.0)
        radius = 0.4 + 0.05 * z
        for column in range(segments):
            angle = 2.0 * math.pi * column / segments
            if alternating and row % 2:
                angle += 0.15 * math.pi / segments
            vertices.append((cx + radius * math.cos(angle), radius * math.sin(angle), z))
    faces = []
    for row in range(intervals):
        for column in range(segments):
            a = row * segments + column
            b = row * segments + (column + 1) % segments
            c = (row + 1) * segments + (column + 1) % segments
            d = (row + 1) * segments + column
            if alternating and (row + column) % 2:
                faces.extend(((a, b, d), (b, c, d)))
            else:
                faces.extend(((a, b, c), (a, c, d)))
    return MeshData(tuple(vertices), tuple(faces))


def _guides() -> tuple[GuideCurveData, GuideCurveData]:
    z = 1.5
    radius = 0.4 + 0.05 * z
    cx = 0.15
    ring = tuple((cx + radius * math.cos(2.0 * math.pi * i / 32),
                  radius * math.sin(2.0 * math.pi * i / 32), z) for i in range(32))
    strip = tuple((0.15 * math.sin(math.pi * height / 3.0) + 0.4 + 0.05 * height,
                   0.0, height) for height in (0.0, 1.5, 3.0))
    return (
        GuideCurveData("REMESH_GUIDE_LOOP_joint", (ring,), kind=("LOOP",), closed=(True,)),
        GuideCurveData("REMESH_GUIDE_STRIP_axis", (strip,), kind=("STRIP",), closed=(False,)),
    )


class TriangulatedLimbIntegrationTests(unittest.TestCase):
    def test_engine_routes_new_tube_grid_without_legacy_fallback(self):
        source = _tube(13, 8, alternating=False)
        result = RemeshBackend().remesh(build_engine_input(
            source,
            RemeshSettings(target_quad_count=256, topology_mode="STRUCTURED"),
            guide_curves=_guides(),
        ))
        self.assertEqual(result.quality.actual_quad_count, 256)
        self.assertFalse(any("실험 엔진" in warning for warning in result.warnings))

    def test_new_regular_rings_on_two_triangle_patterns(self):
        guides = _guides()
        for segments, intervals, alternating in ((13, 8, False), (18, 10, True)):
            with self.subTest(segments=segments, intervals=intervals):
                source = _tube(segments, intervals, alternating=alternating)
                target = 256
                result = try_remesh_triangulated_limb(build_engine_input(
                    source,
                    RemeshSettings(target_quad_count=target, topology_mode="STRUCTURED"),
                    guide_curves=guides,
                ))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_ratio, 1.0)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertLessEqual(abs(analysis.quad_count - target) / target, 0.05)
                report = validate_layout(result, LayoutExpectations(
                    loops=(EdgePathExpectation("관절", guides[0].splines[0], closed=True, tolerance=0.13),),
                    strips=(EdgePathExpectation("축", guides[1].splines[0], closed=False, tolerance=0.13,
                                                endpoints_on_boundary=True),),
                    max_face_aspect_ratio=5.0,
                ))
                self.assertTrue(report.ok, report.issues)
                distance = measure_bidirectional_sample_distance(source, result)
                self.assertLess(distance.max_distance, 0.08)


if __name__ == "__main__":
    unittest.main()
