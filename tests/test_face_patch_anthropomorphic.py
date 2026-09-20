"""합성 돌출형 얼굴에서 여섯 독립 가이드 루프의 격자 품질을 검증한다."""

from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, build_engine_input
from addon.surface import SurfaceIndex
from addon.topology.face_patch import _centroid, _ray_point, _triangulate
from addon.topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout
from tests.test_face_patch_integration import _sphere_mesh


def _gaussian(x: float, y: float, cx: float, cy: float, sx: float, sy: float) -> float:
    return math.exp(-0.5 * (((x - cx) / sx) ** 2 + ((y - cy) / sy) ** 2))


def _face_fixture() -> tuple[MeshData, tuple[GuideCurveData, ...]]:
    source = _sphere_mesh(62, 40, alternating=True)
    vertices = []
    for x, y, z in source.vertices:
        radius = 1.0
        for side in (-1, 1):
            eye_x, eye_y = side * 0.34, 0.24
            eye_ring = math.sqrt(((x - eye_x) / 0.135) ** 2 + ((y - eye_y) / 0.088) ** 2)
            radius += 0.045 * math.exp(-0.5 * ((eye_ring - 1.0) / 0.20) ** 2)
            radius -= 0.038 * _gaussian(x, y, eye_x, eye_y, 0.07, 0.045)
            radius += 0.26 * math.exp(
                -0.5 * (((x - side * 0.96) / 0.13) ** 2 + (y / 0.19) ** 2 + (z / 0.20) ** 2)
            )
        radius += 0.25 * _gaussian(x, y, 0.0, -0.055, 0.085, 0.13) * max(0.0, z)
        radius += 0.07 * _gaussian(x, y, 0.0, -0.315, 0.16, 0.025) * max(0.0, z)
        radius += 0.07 * _gaussian(x, y, 0.0, -0.395, 0.16, 0.025) * max(0.0, z)
        vertices.append((radius * x, radius * y, radius * z))
    mesh = MeshData(tuple(vertices), source.faces)
    surface = SurfaceIndex(_triangulate(mesh))
    origin = _centroid(mesh.vertices)

    def loop(name: str, center: tuple[float, float], radii: tuple[float, float], side: int = 0) -> GuideCurveData:
        points = []
        for index in range(32):
            angle = 2 * math.pi * index / 32
            u = center[0] + radii[0] * math.cos(angle)
            v = center[1] + radii[1] * math.sin(angle)
            direction = (u, v, 1.0) if side == 0 else (float(side), u, v)
            point = _ray_point(surface, origin, direction)
            if point is None:
                raise AssertionError(f"{name} 가이드 광선이 합성 표면을 놓쳤습니다.")
            points.append(point)
        return GuideCurveData(name, (tuple(points),), kind=("LOOP",), closed=(True,))

    guides = (
        loop("left_eye_outer", (-0.34, 0.24), (0.13, 0.085)),
        loop("right_eye_outer", (0.34, 0.24), (0.13, 0.085)),
        loop("nose_base", (0.0, -0.06), (0.075, 0.09)),
        loop("mouth_outer", (0.0, -0.355), (0.17, 0.075)),
        loop("left_ear", (0.0, 0.0), (0.13, 0.14), -1),
        loop("right_ear", (0.0, 0.0), (0.13, 0.14), 1),
    )
    return mesh, guides


class AnthropomorphicFaceTests(unittest.TestCase):
    def test_six_guide_cycles_remain_continuous_on_protruding_face(self):
        source, guides = _face_fixture()
        self.assertTrue(all(len(face) == 3 for face in source.faces))
        for target in (5000, 8000):
            with self.subTest(target=target):
                result = RemeshBackend().remesh(build_engine_input(
                    source,
                    RemeshSettings(target_quad_count=target, topology_mode="STRUCTURED"),
                    guides,
                ))
                self.assertTrue(all(len(face) == 4 for face in result.mesh.faces))
                self.assertLessEqual(abs(result.quality.actual_quad_count - target) / target, 0.05)
                report = validate_layout(result.mesh, LayoutExpectations(
                    loops=tuple(EdgePathExpectation(guide.name, guide.splines[0], True, 0.06) for guide in guides),
                    boundary_edge_count=0,
                    max_face_aspect_ratio=5.0,
                ))
                self.assertTrue(report.ok, report.issues)
                self.assertEqual(report.metrics.bowtie_vertex_count, 0)
                self.assertLessEqual(measure_bidirectional_sample_distance(source, result.mesh).max_distance, 0.04)


if __name__ == "__main__":
    unittest.main()
