"""기존 쿼드 복원에 의존하지 않는 얼굴 루프 생성 수용 검사."""

from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, analyze_mesh, build_engine_input
from addon.large_mesh import needs_preprocessing, remesh_large
from addon.topology.face_patch import try_remesh_face_patch
from addon.topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout


def _sphere_mesh(longitudes: int, latitudes: int, *, alternating: bool) -> MeshData:
    vertices = [(0.0, 0.0, 1.0)]
    for row in range(1, latitudes):
        theta = math.pi * row / latitudes
        for column in range(longitudes):
            phi = 2.0 * math.pi * column / longitudes
            if alternating and row % 2:
                phi += 0.21 * math.pi / longitudes
            vertices.append((
                math.sin(theta) * math.cos(phi),
                math.sin(theta) * math.sin(phi),
                math.cos(theta),
            ))
    south = len(vertices)
    vertices.append((0.0, 0.0, -1.0))

    def ring(row: int, column: int) -> int:
        return 1 + (row - 1) * longitudes + column % longitudes

    faces: list[tuple[int, int, int]] = []
    for column in range(longitudes):
        faces.append((0, ring(1, column + 1), ring(1, column)))
    for row in range(1, latitudes - 1):
        for column in range(longitudes):
            first = ring(row, column)
            second = ring(row, column + 1)
            third = ring(row + 1, column + 1)
            fourth = ring(row + 1, column)
            if alternating and (row + column) % 2:
                faces.extend(((first, second, fourth), (second, third, fourth)))
            else:
                faces.extend(((first, second, third), (first, third, fourth)))
    for column in range(longitudes):
        faces.append((ring(latitudes - 1, column), ring(latitudes - 1, column + 1), south))
    return MeshData(tuple(vertices), tuple(tuple(reversed(face)) for face in faces))


def _surface_loop(name: str, center: tuple[float, float], radius: float) -> GuideCurveData:
    points = []
    for index in range(24):
        angle = 2.0 * math.pi * index / 24
        x = center[0] + radius * math.cos(angle)
        y = center[1] + radius * math.sin(angle)
        points.append((x, y, math.sqrt(1.0 - x * x - y * y)))
    return GuideCurveData(name, (tuple(points),), kind=("LOOP",), closed=(True,))


def _face_guides() -> tuple[GuideCurveData, ...]:
    return (
        _surface_loop("REMESH_GUIDE_LOOP_left", (-0.34, 0.25), 0.13),
        _surface_loop("REMESH_GUIDE_LOOP_right", (0.34, 0.25), 0.13),
        _surface_loop("REMESH_GUIDE_LOOP_mouth", (0.0, -0.31), 0.18),
    )


def _side_loop(name: str, side: int, radius: float) -> GuideCurveData:
    points = []
    for index in range(24):
        angle = 2.0 * math.pi * index / 24
        y = radius * math.cos(angle)
        z = radius * math.sin(angle)
        points.append((side * math.sqrt(1.0 - y * y - z * z), y, z))
    return GuideCurveData(name, (tuple(points),), kind=("LOOP",), closed=(True,))


def _extended_face_guides() -> tuple[GuideCurveData, ...]:
    return _face_guides() + (
        _surface_loop("REMESH_GUIDE_LOOP_nose", (0.0, 0.0), 0.065),
        _side_loop("REMESH_GUIDE_LOOP_left_ear", -1, 0.13),
        _side_loop("REMESH_GUIDE_LOOP_right_ear", 1, 0.13),
    )


class FacePatchIntegrationTests(unittest.TestCase):
    def test_six_face_loops_on_three_projection_faces(self):
        guides = _extended_face_guides()
        for longitudes, latitudes, alternating in ((31, 17, False), (38, 20, True)):
            with self.subTest(longitudes=longitudes, latitudes=latitudes, alternating=alternating):
                source = _sphere_mesh(longitudes, latitudes, alternating=alternating)
                result = try_remesh_face_patch(build_engine_input(
                    source,
                    RemeshSettings(target_quad_count=5000, topology_mode="STRUCTURED"),
                    guide_curves=guides,
                ))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_ratio, 1.0)
                self.assertEqual(analysis.boundary_edge_count, 0)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertLessEqual(abs(analysis.quad_count - 5000) / 5000, 0.05)
                report = validate_layout(result, LayoutExpectations(
                    loops=tuple(EdgePathExpectation(guide.name, guide.splines[0], True, 0.06) for guide in guides),
                    max_face_aspect_ratio=5.0,
                ))
                self.assertTrue(report.ok, report.issues)
                distance = measure_bidirectional_sample_distance(source, result)
                self.assertLess(distance.max_distance, 0.06)

    def test_large_triangle_source_uses_original_surface(self):
        source = _sphere_mesh(110, 100, alternating=True)
        self.assertTrue(needs_preprocessing(source))
        result = remesh_large(build_engine_input(
            source, RemeshSettings(target_quad_count=5000, topology_mode="STRUCTURED"),
            guide_curves=_face_guides(),
        ))
        self.assertEqual(result.quality.actual_quad_count, 4892)
        self.assertLess(result.quality.max_surface_error, 0.01)
        self.assertFalse(any("프록시" in warning for warning in result.warnings))

    def test_engine_routes_new_face_grid_without_legacy_fallback(self):
        source = _sphere_mesh(31, 17, alternating=False)
        result = RemeshBackend().remesh(build_engine_input(
            source,
            RemeshSettings(target_quad_count=2400, topology_mode="STRUCTURED"),
            guide_curves=_face_guides(),
        ))
        self.assertLessEqual(abs(result.quality.actual_quad_count - 2400) / 2400, 0.05)
        self.assertFalse(any("실험 엔진" in warning for warning in result.warnings))

    def test_new_face_cycles_on_unrelated_triangle_spheres(self):
        guides = _face_guides()
        for longitudes, latitudes, alternating in ((31, 17, False), (38, 20, True)):
            with self.subTest(longitudes=longitudes, latitudes=latitudes, alternating=alternating):
                source = _sphere_mesh(longitudes, latitudes, alternating=alternating)
                requested = 2400
                result = try_remesh_face_patch(build_engine_input(
                    source,
                    RemeshSettings(target_quad_count=requested, topology_mode="STRUCTURED"),
                    guide_curves=guides,
                ))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_ratio, 1.0)
                self.assertEqual(analysis.boundary_edge_count, 0)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertLessEqual(abs(analysis.quad_count - requested) / requested, 0.05)
                for face in result.faces:
                    a, b, c = (result.vertices[index] for index in face[:3])
                    ab = tuple(b[axis] - a[axis] for axis in range(3))
                    ac = tuple(c[axis] - a[axis] for axis in range(3))
                    normal = (
                        ab[1] * ac[2] - ab[2] * ac[1],
                        ab[2] * ac[0] - ab[0] * ac[2],
                        ab[0] * ac[1] - ab[1] * ac[0],
                    )
                    center = tuple(sum(result.vertices[index][axis] for index in face) / 4 for axis in range(3))
                    self.assertGreater(sum(normal[axis] * center[axis] for axis in range(3)), 0.0)
                expectations = LayoutExpectations(loops=tuple(
                    EdgePathExpectation(guide.name, guide.splines[0], closed=True, tolerance=0.06)
                    for guide in guides
                ), max_face_aspect_ratio=5.0)
                report = validate_layout(result, expectations)
                self.assertTrue(report.ok, report.issues)
                distance = measure_bidirectional_sample_distance(source, result)
                self.assertLess(distance.max_distance, 0.06)


if __name__ == "__main__":
    unittest.main()
