from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshCancelled, RemeshSettings, build_engine_input
from addon.topology.periodic import try_remesh_periodic
from addon.topology.planar import try_remesh_planar
from addon.topology.quality import (
    EdgePathExpectation,
    LayoutExpectations,
    RegularChartExpectation,
    measure_bidirectional_sample_distance,
    validate_layout,
)


class TopologyQualityTests(unittest.TestCase):
    def test_legacy_cube_target_384_reports_unintended_regular_chart_poles(self):
        result = RemeshBackend().remesh(
            build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384, topology_mode="LEGACY"))
        )

        report = validate_layout(
            result.mesh,
            LayoutExpectations(
                regular_charts=_cube_regular_charts(result.mesh, expected_quad_count=64),
                boundary_edge_count=0,
                max_face_aspect_ratio=1.05,
            ),
        )

        self.assertFalse(report.ok)
        self.assertIn("CHART_UNINTENDED_POLE", _issue_codes(report))
        self.assertIn("CHART_QUAD_COUNT", _issue_codes(report))

    def test_planar_cube_target_384_passes_regular_chart_quality(self):
        result = try_remesh_planar(build_engine_input(_cube_mesh(), RemeshSettings(target_quad_count=384)))

        self.assertIsNotNone(result)
        assert result is not None
        report = validate_layout(
            result,
            LayoutExpectations(
                regular_charts=_cube_regular_charts(result, expected_quad_count=64),
                boundary_edge_count=0,
                max_face_aspect_ratio=1.0,
            ),
        )

        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.metrics.non_manifold_edge_count, 0)
        self.assertEqual({chart.unintended_pole_count for chart in report.metrics.chart_metrics}, {0})

    def test_tube_loop_and_strip_paths_have_output_edge_correspondence(self):
        source = _regular_tube_mesh()
        loop = GuideCurveData(
            name="REMESH_GUIDE_LOOP_section",
            splines=(_circle_loop(1.0, 1.4, 12),),
            kind=("LOOP",),
            closed=(True,),
        )
        strip = GuideCurveData(
            name="REMESH_GUIDE_STRIP_side",
            splines=(_strip_line(2, 9),),
            kind=("STRIP",),
            closed=(False,),
        )
        result = try_remesh_periodic(
            build_engine_input(source, RemeshSettings(target_quad_count=18), guide_curves=(loop, strip))
        )

        self.assertIsNotNone(result)
        assert result is not None
        report = validate_layout(
            result,
            LayoutExpectations(
                loops=(EdgePathExpectation("section", _row_points(result, 1, 6), closed=True, tolerance=1.0e-8),),
                strips=(
                    EdgePathExpectation(
                        "side",
                        _column_points(result, 0, 4, 6),
                        closed=False,
                        tolerance=1.0e-8,
                        endpoints_on_boundary=True,
                    ),
                ),
                boundary_edge_count=12,
            ),
        )

        self.assertTrue(report.ok, report.issues)

    def test_loop_path_allows_intermediate_output_vertices_between_guide_samples(self):
        mesh = _regular_tube_mesh()
        sparse_loop = tuple(mesh.vertices[index] for index in (6, 8, 10))

        report = validate_layout(
            mesh,
            LayoutExpectations(
                loops=(EdgePathExpectation("sparse_section", sparse_loop, closed=True, tolerance=1.0e-8),),
                boundary_edge_count=12,
            ),
        )

        self.assertTrue(report.ok, report.issues)

    def test_spokes_do_not_count_as_a_closed_loop(self):
        vertices = (
            (0.5, 0.5, 0.0),
            (0.0, 0.0, 0.0), (0.5, 0.0, 2.0),
            (1.0, 0.0, 0.0), (1.0, 0.5, 2.0),
            (1.0, 1.0, 0.0), (0.5, 1.0, 2.0),
            (0.0, 1.0, 0.0), (0.0, 0.5, 2.0),
        )
        faces = tuple((0, index, index + 1) for index in range(1, 8)) + ((0, 8, 1),)
        mesh = MeshData(vertices, faces)
        guide = tuple(vertices[index] for index in (1, 3, 5, 7))
        report = validate_layout(
            mesh,
            LayoutExpectations(loops=(EdgePathExpectation("가짜 폐루프", guide, closed=True, tolerance=0.55),)),
        )
        self.assertFalse(report.ok)
        self.assertIn("LOOP_EDGE_MISSING", {issue.code for issue in report.issues})

    def test_bowtie_vertex_is_reported_when_face_fans_split(self):
        mesh = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (-1, 0, 0), (-1, -1, 0), (0, -1, 0)),
            faces=((0, 1, 2, 3), (0, 4, 5, 6)),
        )

        report = validate_layout(mesh, LayoutExpectations(max_bowtie_vertices=0))

        self.assertFalse(report.ok)
        self.assertIn("BOWTIE_VERTEX", _issue_codes(report))
        self.assertEqual(report.metrics.bowtie_vertex_count, 1)

    def test_bidirectional_sample_distance_is_zero_for_identical_meshes(self):
        mesh = _cube_mesh()

        distance = measure_bidirectional_sample_distance(mesh, mesh)

        self.assertLess(distance.max_distance, 1.0e-12)
        self.assertEqual(distance.source_sample_count, len(mesh.vertices) + len(mesh.faces))
        self.assertEqual(distance.output_sample_count, len(mesh.vertices) + len(mesh.faces))

    def test_bidirectional_sample_distance_uses_surface_not_point_cloud_only(self):
        source = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
            faces=((0, 1, 2), (0, 2, 3)),
        )
        output = MeshData(vertices=source.vertices, faces=((0, 1, 2, 3),))

        distance = measure_bidirectional_sample_distance(source, output, max_samples_per_mesh=3)

        self.assertLess(distance.max_distance, 1.0e-12)
        self.assertEqual(distance.source_sample_count, 3)
        self.assertEqual(distance.output_sample_count, 3)

    def test_bidirectional_sample_distance_uses_ear_clipped_concave_ngon_surface(self):
        source = MeshData(
            vertices=((0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 0.35, 0), (0, 1, 0)),
            faces=((0, 1, 2, 3, 4),),
        )
        output = MeshData(
            vertices=((0.9, 0.82, 0), (1.1, 0.82, 0), (1.0, 0.9, 0)),
            faces=((0, 1, 2),),
        )

        distance = measure_bidirectional_sample_distance(source, output)

        self.assertGreater(distance.max_output_to_source, 0.3)

    def test_bidirectional_sample_distance_catches_missing_source_detail(self):
        source = MeshData(
            vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0.5, 0.5, 1.0)),
            faces=((0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)),
        )
        output = MeshData(vertices=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), faces=((0, 1, 2, 3),))

        distance = measure_bidirectional_sample_distance(source, output, chunk_size=2)

        self.assertGreater(distance.max_source_to_output, 0.9)
        self.assertGreater(distance.max_output_to_source, 0.0)

    def test_bidirectional_sample_distance_observes_cancellation(self):
        with self.assertRaises(RemeshCancelled):
            measure_bidirectional_sample_distance(_cube_mesh(), _cube_mesh(), cancelled=lambda: True, chunk_size=1)


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


def _cube_regular_charts(mesh: MeshData, *, expected_quad_count: int) -> tuple[RegularChartExpectation, ...]:
    charts = []
    for axis in range(3):
        for side in (-1.0, 1.0):
            face_indices = tuple(
                index
                for index, face in enumerate(mesh.faces)
                if all(abs(mesh.vertices[vertex][axis] - side) < 1.0e-6 for vertex in face)
            )
            charts.append(
                RegularChartExpectation(
                    name=f"axis{axis}_{side:g}",
                    face_indices=face_indices,
                    expected_quad_count=expected_quad_count,
                    max_unintended_poles=0,
                    max_edge_spacing_cv=1.0e-8,
                    max_face_aspect_ratio=1.05,
                )
            )
    return tuple(charts)


def _regular_tube_mesh() -> MeshData:
    angles = tuple(2.0 * math.pi * index / 6 for index in range(6))
    heights = (0.0, 0.2, 1.4, 3.0)
    vertices = tuple((math.cos(angle), math.sin(angle), height) for height in heights for angle in angles)
    faces = []
    for row in range(len(heights) - 1):
        for segment in range(len(angles)):
            faces.append(
                (
                    row * len(angles) + segment,
                    row * len(angles) + (segment + 1) % len(angles),
                    (row + 1) * len(angles) + (segment + 1) % len(angles),
                    (row + 1) * len(angles) + segment,
                )
            )
    return MeshData(vertices=vertices, faces=tuple(faces))


def _circle_loop(radius: float, height: float, count: int) -> tuple[tuple[float, float, float], ...]:
    return tuple((radius * math.cos(2.0 * math.pi * index / count), radius * math.sin(2.0 * math.pi * index / count), height) for index in range(count))


def _strip_line(segment: int, count: int) -> tuple[tuple[float, float, float], ...]:
    angle = 2.0 * math.pi * segment / 6
    return tuple((math.cos(angle), math.sin(angle), 3.0 * index / (count - 1)) for index in range(count))


def _row_points(mesh: MeshData, row: int, ring_size: int) -> tuple[tuple[float, float, float], ...]:
    start = row * ring_size
    return mesh.vertices[start : start + ring_size]


def _column_points(mesh: MeshData, segment: int, row_count: int, ring_size: int) -> tuple[tuple[float, float, float], ...]:
    return tuple(mesh.vertices[row * ring_size + segment] for row in range(row_count))


def _issue_codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


if __name__ == "__main__":
    unittest.main()
