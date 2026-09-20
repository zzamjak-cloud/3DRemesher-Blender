"""기존 모델을 열지 않고 합성 불규칙 삼각 관의 새 쿼드 격자를 검증한다."""

from __future__ import annotations

import math
import time
import unittest

from addon.core import (
    GuideCurveData, MeshData, RemeshBackend, RemeshCancelled, RemeshSettings,
    analyze_mesh, build_engine_input,
)
from addon.topology.irregular_tube import try_remesh_irregular_tube
from addon.topology.quality import (
    EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance,
    validate_layout,
)


def irregular_tube(segments: int = 17, rows: int = 14, *, alternative: bool = False,
                   reverse: bool = False) -> MeshData:
    vertices = []
    for row in range(rows):
        for column in range(segments):
            height = 3.0 * row / (rows - 1)
            if row not in (0, rows - 1):
                height += 0.035 * math.sin(column * 2.31 + row * 1.17)
            angle = 2.0 * math.pi * column / segments
            if alternative and row not in (0, rows - 1):
                angle += 0.13 * math.sin(row * 0.7) / segments
            radius = 0.4 + 0.055 * height
            center_x = 0.13 * math.sin(math.pi * height / 3.0)
            vertices.append((center_x + radius * math.cos(angle), radius * math.sin(angle), height))
    faces = []
    for row in range(rows - 1):
        for column in range(segments):
            a = row * segments + column
            b = row * segments + (column + 1) % segments
            c = (row + 1) * segments + (column + 1) % segments
            d = (row + 1) * segments + column
            if (row + column + int(alternative)) % 2:
                faces.extend(((a, b, d), (b, c, d)))
            else:
                faces.extend(((a, b, c), (a, c, d)))
    if reverse:
        faces = [tuple(reversed(face)) for face in faces]
    return MeshData(tuple(vertices), tuple(faces))


def guides() -> tuple[GuideCurveData, GuideCurveData]:
    height = 1.5
    radius = 0.4 + 0.055 * height
    loop = tuple((0.13 + radius * math.cos(2 * math.pi * index / 32),
                  radius * math.sin(2 * math.pi * index / 32), height) for index in range(32))
    strip = tuple((0.13 * math.sin(math.pi * height / 3.0) + 0.4 + 0.055 * height,
                   0.0, height) for height in (0.0, 1.5, 3.0))
    return (
        GuideCurveData("관절", (loop,), kind=("LOOP",), closed=(True,)),
        GuideCurveData("길이", (strip,), kind=("STRIP",), closed=(False,)),
    )


def face_centroid_triangulation(source: MeshData) -> MeshData:
    vertices = list(source.vertices)
    faces = []
    for a, b, c in source.faces:
        points = (source.vertices[a], source.vertices[b], source.vertices[c])
        center = tuple(sum(point[axis] for point in points) / 3.0 for axis in range(3))
        index = len(vertices)
        vertices.append(center)
        faces.extend(((a, b, index), (b, c, index), (c, a, index)))
    return MeshData(tuple(vertices), tuple(faces))


def tapered_tube(end_radius: float) -> MeshData:
    source = irregular_tube()
    vertices = []
    for x, y, height in source.vertices:
        center_x = 0.13 * math.sin(math.pi * height / 3.0)
        original_radius = 0.4 + 0.055 * height
        radius = 0.4 + (end_radius - 0.4) * height / 3.0
        scale = radius / original_radius
        vertices.append((center_x + (x - center_x) * scale, y * scale, height))
    return MeshData(tuple(vertices), source.faces)


class IrregularTubeTests(unittest.TestCase):
    def test_engine_routes_irregular_tube_without_legacy_fallback(self):
        source = irregular_tube(alternative=True)
        result = RemeshBackend().remesh(build_engine_input(
            source, RemeshSettings(target_quad_count=256, topology_mode="STRUCTURED"),
            guide_curves=guides(),
        ))
        self.assertEqual(result.quality.actual_quad_count, 256)
        self.assertLess(result.quality.max_aspect_ratio, 5.0)
        self.assertFalse(any("실험 엔진" in warning for warning in result.warnings))

    def test_different_irregular_triangulations_yield_regular_quad_rings(self):
        for alternative, refined in ((False, False), (True, False), (True, True)):
            with self.subTest(alternative=alternative, refined=refined):
                source = irregular_tube(alternative=alternative)
                if refined:
                    source = face_centroid_triangulation(source)
                result = try_remesh_irregular_tube(build_engine_input(
                    source, RemeshSettings(target_quad_count=256), guide_curves=guides()))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_ratio, 1.0)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertLessEqual(abs(analysis.quad_count - 256) / 256, 0.05)
                expectation = LayoutExpectations(
                    loops=(EdgePathExpectation("관절", guides()[0].splines[0], True, tolerance=0.16),),
                    strips=(EdgePathExpectation("길이", guides()[1].splines[0], False,
                                                tolerance=0.16, endpoints_on_boundary=True),),
                    boundary_edge_count=32,
                    max_face_aspect_ratio=5.0,
                    max_edge_spacing_cv=0.15,
                )
                report = validate_layout(result, expectation)
                self.assertTrue(report.ok, report.issues)
                self.assertEqual(report.metrics.bowtie_vertex_count, 0)
                distance = measure_bidirectional_sample_distance(source, result)
                self.assertLess(distance.max_distance, 0.08)

    def test_source_has_no_planar_internal_vertex_ring(self):
        source = irregular_tube()
        internal_heights = [point[2] for point in source.vertices[17:-17]]
        self.assertGreater(len({round(height, 5) for height in internal_heights}), 17)

    def test_reverse_winding_and_rotated_axis(self):
        source = irregular_tube(reverse=True)
        angle = 0.6
        vertices = tuple((2 + x * math.cos(angle) + z * math.sin(angle),
                          y - 1, 0.4 - x * math.sin(angle) + z * math.cos(angle))
                         for x, y, z in source.vertices)
        result = try_remesh_irregular_tube(build_engine_input(
            MeshData(vertices, source.faces), RemeshSettings(target_quad_count=256)))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 256)

    def test_wrong_guides_and_unsupported_controls_rejected(self):
        source = irregular_tube()
        wrong = GuideCurveData("잘못된 관절", (((10, 0, 1), (10, 1, 1), (11, 1, 1), (11, 0, 1)),),
                               kind=("LOOP",), closed=(True,))
        self.assertIsNone(try_remesh_irregular_tube(build_engine_input(
            source, RemeshSettings(target_quad_count=256), guide_curves=(wrong,))))
        self.assertIsNone(try_remesh_irregular_tube(build_engine_input(
            source, RemeshSettings(target_quad_count=256, symmetry_axes=("X",)))))

    def test_loop_ten_centimeters_inside_source_is_rejected(self):
        source = irregular_tube()
        loop = guides()[0].splines[0]
        center_x = 0.13
        inside = tuple((center_x + (x - center_x) * (1.0 - 0.10 / 0.4825),
                        y * (1.0 - 0.10 / 0.4825), z) for x, y, z in loop)
        guide = GuideCurveData("표면 안쪽 루프", (inside,), kind=("LOOP",), closed=(True,))
        engine_input = build_engine_input(
            source, RemeshSettings(target_quad_count=256, topology_mode="STRUCTURED"),
            guide_curves=(guide,),
        )
        self.assertIsNone(try_remesh_irregular_tube(engine_input))
        with self.assertRaisesRegex(ValueError, "표면 안쪽 루프"):
            RemeshBackend().remesh(engine_input)

    def test_branch_or_closed_surface_rejected(self):
        source = irregular_tube()
        branched = MeshData(source.vertices + ((1.0, 0.0, 1.0),),
                            source.faces + ((0, 1, len(source.vertices)),))
        self.assertIsNone(try_remesh_irregular_tube(build_engine_input(
            branched, RemeshSettings(target_quad_count=256))))

    def test_cancellation_during_slicing(self):
        source = irregular_tube(32, 50)
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 80

        with self.assertRaises(RemeshCancelled):
            try_remesh_irregular_tube(build_engine_input(
                source, RemeshSettings(target_quad_count=5000)), cancelled=cancelled)

    def test_large_input_bounded_runtime(self):
        source = irregular_tube(48, 64)
        started = time.monotonic()
        result = try_remesh_irregular_tube(build_engine_input(
            source, RemeshSettings(target_quad_count=5000)))
        elapsed = time.monotonic() - started
        self.assertIsNotNone(result)
        self.assertLess(elapsed, 20.0)
        assert result is not None
        self.assertLessEqual(abs(len(result.faces) - 5000) / 5000, 0.05)

    def test_dense_boundary_runtime(self):
        for segments, limit in ((800, 15.0), (1600, 30.0)):
            with self.subTest(segments=segments):
                source = irregular_tube(segments, 2)
                started = time.monotonic()
                result = try_remesh_irregular_tube(build_engine_input(
                    source, RemeshSettings(target_quad_count=5000)))
                elapsed = time.monotonic() - started
                self.assertIsNotNone(result)
                self.assertLess(elapsed, limit, f"{segments} 둘레 정점 처리: {elapsed:.2f}초")

    def test_extreme_taper_rejected_before_engine_quality_gate(self):
        moderate = try_remesh_irregular_tube(build_engine_input(
            tapered_tube(0.2), RemeshSettings(target_quad_count=256)))
        self.assertIsNotNone(moderate)
        extreme = try_remesh_irregular_tube(build_engine_input(
            tapered_tube(0.025), RemeshSettings(target_quad_count=256)))
        self.assertIsNone(extreme)


if __name__ == "__main__":
    unittest.main()
