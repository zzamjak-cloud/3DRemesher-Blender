from __future__ import annotations

import math
import unittest

from addon.core import (
    GuideCurveData,
    MeshData,
    RemeshBackend,
    RemeshCancelled,
    RemeshSettings,
    analyze_mesh,
    build_engine_input,
)
from addon.topology.periodic import try_remesh_periodic


class PeriodicTopologyTests(unittest.TestCase):
    def test_tube_grid_extracts_closed_rings_and_even_longitudinal_strips(self):
        mesh = _uneven_tube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18))

        result = try_remesh_periodic(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        analysis = analyze_mesh(result)
        self.assertEqual(analysis.quad_count, 18)
        self.assertEqual(analysis.quad_ratio, 1.0)
        self.assertEqual(analysis.boundary_edge_count, 12)
        self.assertEqual(analysis.non_manifold_edge_count, 0)
        self.assertEqual(len(result.vertices), 24)
        self.assertTrue(_has_closed_ring_edges(result, 0, 6))
        self.assertTrue(_has_closed_ring_edges(result, 1, 6))
        self.assertTrue(_has_closed_ring_edges(result, 2, 6))
        self.assertTrue(_has_closed_ring_edges(result, 3, 6))
        self.assertTrue(_has_longitudinal_strips(result, 4, 6))
        self.assertLess(_max_ring_spacing_error(result, 6), 1.0e-6)
        self.assertLess(_max_center_step_error(result, 6), 1.0e-6)

    def test_rejects_non_tube_quad_topology(self):
        mesh = MeshData(
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
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=24))

        self.assertIsNone(try_remesh_periodic(engine_input))

    def test_loop_guide_pins_matching_cross_section_to_output_row(self):
        mesh = _uneven_tube_mesh()
        guide = GuideCurveData(
            name="REMESH_GUIDE_LOOP_section",
            splines=(_circle_loop(1.0, 1.4, 12),),
            kind=("LOOP",),
            closed=(True,),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(guide,))

        result = try_remesh_periodic(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(_row_center(result, 1, 6)[2], 1.4)
        self.assertTrue(_has_closed_ring_edges(result, 1, 6))
        self.assertEqual(analyze_mesh(result).quad_count, 18)

    def test_rejects_direction_guides_instead_of_ignoring_them(self):
        mesh = _uneven_tube_mesh()
        guide = GuideCurveData(name="REMESH_GUIDE_axis", splines=(((0, 0, 0), (0, 0, 3)),))
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(guide,))

        self.assertIsNone(try_remesh_periodic(engine_input))

    def test_strip_guide_pins_matching_longitudinal_edge_chain(self):
        mesh = _uneven_tube_mesh()
        guide = GuideCurveData(
            name="REMESH_GUIDE_STRIP_side",
            splines=(_strip_line(2, 9),),
            kind=("STRIP",),
            closed=(False,),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(guide,))

        result = try_remesh_periodic(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(_has_longitudinal_chain_edges(result, 0, 4, 6))
        for row in range(4):
            vertex = result.vertices[row * 6]
            self.assertAlmostEqual(vertex[0], math.cos(2.0 * math.pi * 2 / 6))
            self.assertAlmostEqual(vertex[1], math.sin(2.0 * math.pi * 2 / 6))
        self.assertEqual(analyze_mesh(result).quad_count, 18)

    def test_auto_entry_uses_periodic_strip_quality(self):
        mesh = _regular_tube_mesh()
        guide = GuideCurveData(
            name="REMESH_GUIDE_STRIP_side",
            splines=(_strip_line(2, 9),),
            kind=("STRIP",),
            closed=(False,),
        )
        engine_input = build_engine_input(
            mesh,
            RemeshSettings(target_quad_count=18, topology_mode="AUTO"),
            guide_curves=(guide,),
        )

        result = RemeshBackend().remesh(engine_input)

        self.assertEqual(result.quality.actual_quad_count, 18)
        self.assertEqual(result.quality.quad_ratio, 1.0)
        self.assertEqual(result.quality.boundary_edge_count, 12)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)

    def test_uniform_default_density_values_do_not_block_periodic_path(self):
        mesh = _regular_tube_mesh()
        density_values = (1.0,) * len(mesh.vertices)
        engine_input = build_engine_input(
            mesh,
            RemeshSettings(target_quad_count=18, topology_mode="AUTO"),
            density_values=density_values,
        )

        direct = try_remesh_periodic(engine_input)
        result = RemeshBackend().remesh(engine_input)

        self.assertIsNotNone(direct)
        assert direct is not None
        self.assertEqual(analyze_mesh(direct).quad_count, 18)
        self.assertEqual(result.quality.actual_quad_count, 18)
        self.assertEqual(result.quality.quad_ratio, 1.0)

    def test_varying_density_values_still_reject_periodic_path(self):
        mesh = _regular_tube_mesh()
        density_values = tuple(1.0 if index % 2 == 0 else 1.2 for index in range(len(mesh.vertices)))
        engine_input = build_engine_input(
            mesh,
            RemeshSettings(target_quad_count=18),
            density_values=density_values,
        )

        self.assertIsNone(try_remesh_periodic(engine_input))

    def test_curved_variable_radius_tube_preserves_rings_and_engine_surface_error(self):
        mesh = _curved_variable_radius_tube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=40, topology_mode="AUTO"))

        direct = try_remesh_periodic(engine_input)
        result = RemeshBackend().remesh(engine_input)

        self.assertIsNotNone(direct)
        assert direct is not None
        self.assertEqual(analyze_mesh(direct).quad_count, 40)
        self.assertEqual(result.quality.actual_quad_count, 40)
        self.assertEqual(result.quality.quad_ratio, 1.0)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)
        self.assertLess(result.quality.max_surface_error, 0.01)
        self.assertTrue(_has_longitudinal_strips(result.mesh, 6, 8))
        for row in range(6):
            self.assertTrue(_has_closed_ring_edges(result.mesh, row, 8))

    def test_curved_tube_accepts_matching_loop_and_strip_guides(self):
        mesh = _curved_variable_radius_tube_mesh()
        loop = GuideCurveData(
            name="REMESH_GUIDE_LOOP_elbow",
            splines=(_curved_loop(2, 16),),
            kind=("LOOP",),
            closed=(True,),
        )
        strip = GuideCurveData(
            name="REMESH_GUIDE_STRIP_outer",
            splines=(_curved_strip(0, 11),),
            kind=("STRIP",),
            closed=(False,),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=40), guide_curves=(loop, strip))

        result = try_remesh_periodic(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(_has_closed_ring_edges(result, 2, 8))
        self.assertTrue(_has_longitudinal_chain_edges(result, 0, 6, 8))

    def test_curved_tube_rejects_guides_or_symmetry_that_break_constraints(self):
        mesh = _curved_variable_radius_tube_mesh()
        bad_loop = GuideCurveData(
            name="REMESH_GUIDE_LOOP_far",
            splines=(_translated_loop(_curved_loop(2, 16), (0.0, 0.0, 0.35)),),
            kind=("LOOP",),
            closed=(True,),
        )
        bad_strip = GuideCurveData(
            name="REMESH_GUIDE_STRIP_short",
            splines=(_curved_strip(0, 5)[1:-1],),
            kind=("STRIP",),
            closed=(False,),
        )

        loop_input = build_engine_input(mesh, RemeshSettings(target_quad_count=40), guide_curves=(bad_loop,))
        strip_input = build_engine_input(mesh, RemeshSettings(target_quad_count=40), guide_curves=(bad_strip,))

        self.assertIsNone(try_remesh_periodic(loop_input))
        self.assertIsNone(try_remesh_periodic(strip_input))
        self.assertIsNone(
            try_remesh_periodic(build_engine_input(mesh, RemeshSettings(target_quad_count=40, symmetry_axes=("X",))))
        )

    def test_rejects_ambiguous_or_incomplete_strip_guide(self):
        mesh = _uneven_tube_mesh()
        incomplete = GuideCurveData(
            name="REMESH_GUIDE_STRIP_short",
            splines=(((1.0, 0.0, 0.2), (1.0, 0.0, 2.8)),),
            kind=("STRIP",),
            closed=(False,),
        )
        incomplete_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(incomplete,))

        self.assertIsNone(try_remesh_periodic(incomplete_input))

        first = GuideCurveData(
            name="REMESH_GUIDE_STRIP_a",
            splines=(_strip_line(0, 9),),
            kind=("STRIP",),
            closed=(False,),
        )
        second = GuideCurveData(
            name="REMESH_GUIDE_STRIP_b",
            splines=(_strip_line(2, 9),),
            kind=("STRIP",),
            closed=(False,),
        )
        conflicting_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(first, second))

        self.assertIsNone(try_remesh_periodic(conflicting_input))

    def test_loop_and_strip_guides_can_share_consistent_crossing(self):
        mesh = _uneven_tube_mesh()
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
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(loop, strip))

        result = try_remesh_periodic(engine_input)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(_row_center(result, 1, 6)[2], 1.4)
        self.assertTrue(_has_longitudinal_chain_edges(result, 0, 4, 6))

    def test_rejects_loop_guide_far_from_tube_surface(self):
        mesh = _uneven_tube_mesh()
        guide = GuideCurveData(
            name="REMESH_GUIDE_LOOP_far",
            splines=(_circle_loop(1.4, 1.4, 12),),
            kind=("LOOP",),
            closed=(True,),
        )
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18), guide_curves=(guide,))

        self.assertIsNone(try_remesh_periodic(engine_input))

    def test_cancelled_callback_raises_remesh_cancelled(self):
        mesh = _uneven_tube_mesh()
        engine_input = build_engine_input(mesh, RemeshSettings(target_quad_count=18))

        with self.assertRaises(RemeshCancelled):
            try_remesh_periodic(engine_input, cancelled=lambda: True)


def _uneven_tube_mesh() -> MeshData:
    angles = (0.0, 0.35, 1.7, 3.1, 4.4, 5.2)
    heights = (0.0, 0.2, 1.4, 3.0)
    return _tube_mesh(angles, heights)


def _regular_tube_mesh() -> MeshData:
    angles = tuple(2.0 * math.pi * index / 6 for index in range(6))
    heights = (0.0, 0.2, 1.4, 3.0)
    return _tube_mesh(angles, heights)


def _tube_mesh(angles: tuple[float, ...], heights: tuple[float, ...]) -> MeshData:
    vertices = []
    for height in heights:
        for angle in angles:
            vertices.append((math.cos(angle), math.sin(angle), height))
    faces = []
    ring_size = len(angles)
    for row in range(len(heights) - 1):
        for segment in range(ring_size):
            faces.append(
                (
                    row * ring_size + segment,
                    row * ring_size + (segment + 1) % ring_size,
                    (row + 1) * ring_size + (segment + 1) % ring_size,
                    (row + 1) * ring_size + segment,
                )
            )
    return MeshData(vertices=tuple(vertices), faces=tuple(faces))


def _curved_variable_radius_tube_mesh() -> MeshData:
    return _tube_from_rings(tuple(_curved_ring(row, 8) for row in range(6)))


def _curved_ring(row: int, segments: int) -> tuple[tuple[float, float, float], ...]:
    center = _curved_center(row)
    normal, binormal = _curved_frame(row)
    radius = 0.34 - 0.035 * row
    return tuple(
        (
            center[0] + radius * _ring_offset(segment, segments, normal, binormal)[0],
            center[1] + radius * _ring_offset(segment, segments, normal, binormal)[1],
            center[2] + radius * _ring_offset(segment, segments, normal, binormal)[2],
        )
        for segment in range(segments)
    )


def _ring_offset(segment: int, segments: int, normal, binormal) -> tuple[float, float, float]:
    angle = 2.0 * math.pi * segment / segments
    return tuple(math.cos(angle) * normal[axis] + math.sin(angle) * binormal[axis] for axis in range(3))


def _tube_from_rings(rings: tuple[tuple[tuple[float, float, float], ...], ...]) -> MeshData:
    vertices = tuple(vertex for ring in rings for vertex in ring)
    faces = []
    ring_size = len(rings[0])
    for row in range(len(rings) - 1):
        for segment in range(ring_size):
            faces.append(
                (
                    row * ring_size + segment,
                    row * ring_size + (segment + 1) % ring_size,
                    (row + 1) * ring_size + (segment + 1) % ring_size,
                    (row + 1) * ring_size + segment,
                )
            )
    return MeshData(vertices=vertices, faces=tuple(faces))


def _curved_center(row: int) -> tuple[float, float, float]:
    t = row / 5.0
    angle = 0.95 * t
    return (1.4 * math.sin(angle), 0.2 * math.sin(1.7 * angle), 1.4 * (1.0 - math.cos(angle)))


def _curved_frame(row: int) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    t = row / 5.0
    angle = 0.95 * t
    tangent = _unit((1.4 * math.cos(angle), 0.34 * math.cos(1.7 * angle), 1.4 * math.sin(angle)))
    up = (0.0, 1.0, 0.0)
    normal = _unit(_cross(up, tangent))
    binormal = _unit(_cross(tangent, normal))
    return normal, binormal


def _curved_loop(row: int, count: int) -> tuple[tuple[float, float, float], ...]:
    ring = _curved_ring(row, count)
    return (*ring, ring[0])


def _curved_strip(segment: int, count: int) -> tuple[tuple[float, float, float], ...]:
    rows = tuple(_curved_ring(row, 8)[segment] for row in range(6))
    samples = []
    for sample in range(count):
        position = sample * (len(rows) - 1) / (count - 1)
        index = min(len(rows) - 2, int(math.floor(position)))
        alpha = position - index
        samples.append(_lerp(rows[index], rows[index + 1], alpha))
    return tuple(samples)


def _translated_loop(
    loop: tuple[tuple[float, float, float], ...],
    offset: tuple[float, float, float],
) -> tuple[tuple[float, float, float], ...]:
    return tuple((point[0] + offset[0], point[1] + offset[1], point[2] + offset[2]) for point in loop)


def _circle_loop(radius: float, height: float, count: int) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        (
            radius * math.cos(2.0 * math.pi * index / count),
            radius * math.sin(2.0 * math.pi * index / count),
            height,
        )
        for index in range(count)
    )


def _strip_line(segment: int, count: int) -> tuple[tuple[float, float, float], ...]:
    angle = 2.0 * math.pi * segment / 6
    return tuple(
        (
            math.cos(angle),
            math.sin(angle),
            3.0 * index / (count - 1),
        )
        for index in range(count)
    )


def _edge_set(mesh: MeshData) -> set[tuple[int, int]]:
    edges = set()
    for face in mesh.faces:
        for first, second in zip(face, (*face[1:], face[0])):
            edges.add(tuple(sorted((first, second))))
    return edges


def _has_closed_ring_edges(mesh: MeshData, row: int, ring_size: int) -> bool:
    edges = _edge_set(mesh)
    offset = row * ring_size
    return all(
        tuple(sorted((offset + segment, offset + (segment + 1) % ring_size))) in edges
        for segment in range(ring_size)
    )


def _has_longitudinal_strips(mesh: MeshData, row_count: int, ring_size: int) -> bool:
    edges = _edge_set(mesh)
    for row in range(row_count - 1):
        for segment in range(ring_size):
            if tuple(sorted((row * ring_size + segment, (row + 1) * ring_size + segment))) not in edges:
                return False
    return True


def _has_longitudinal_chain_edges(mesh: MeshData, segment: int, row_count: int, ring_size: int) -> bool:
    edges = _edge_set(mesh)
    return all(
        tuple(sorted((row * ring_size + segment, (row + 1) * ring_size + segment))) in edges
        for row in range(row_count - 1)
    )


def _row_center(mesh: MeshData, row: int, ring_size: int) -> tuple[float, float, float]:
    offset = row * ring_size
    points = mesh.vertices[offset : offset + ring_size]
    return tuple(sum(point[axis] for point in points) / ring_size for axis in range(3))


def _max_ring_spacing_error(mesh: MeshData, ring_size: int) -> float:
    maximum = 0.0
    row_count = len(mesh.vertices) // ring_size
    for row in range(row_count):
        offset = row * ring_size
        lengths = [
            _distance(mesh.vertices[offset + segment], mesh.vertices[offset + (segment + 1) % ring_size])
            for segment in range(ring_size)
        ]
        maximum = max(maximum, max(lengths) - min(lengths))
    return maximum


def _max_center_step_error(mesh: MeshData, ring_size: int) -> float:
    centers = []
    row_count = len(mesh.vertices) // ring_size
    for row in range(row_count):
        offset = row * ring_size
        points = mesh.vertices[offset : offset + ring_size]
        centers.append(tuple(sum(point[axis] for point in points) / ring_size for axis in range(3)))
    steps = [_distance(first, second) for first, second in zip(centers, centers[1:])]
    return max(steps) - min(steps)


def _distance(first, second) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _lerp(first, second, alpha: float) -> tuple[float, float, float]:
    return tuple(a + (b - a) * alpha for a, b in zip(first, second))


def _unit(vector) -> tuple[float, float, float]:
    length = _distance(vector, (0.0, 0.0, 0.0))
    return tuple(component / length for component in vector)


def _cross(first, second) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
