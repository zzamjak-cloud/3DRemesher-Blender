"""독립 삼각형 튜브 경로의 위상 및 제어 계약을 확인한다."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from addon.core import GuideCurveData, MeshData, RemeshCancelled, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology import triangulated_limb
from addon.topology.triangulated_limb import try_remesh_triangulated_limb


def tube(*, flip: bool = False, reverse: bool = False,
         segments: int = 12, rows: int = 9) -> MeshData:
    vertices = []
    for row in range(rows):
        z = row / (rows - 1) * 2.4
        center_x = 0.12 * math.sin(math.pi * z / 2.4)
        radius = 0.35 + 0.06 * z
        for segment in range(segments):
            angle = 2 * math.pi * segment / segments + (0.04 if row % 2 else 0.0)
            vertices.append((center_x + radius * math.cos(angle), radius * math.sin(angle), z))
    faces = []
    for row in range(rows - 1):
        for column in range(segments):
            a = row * segments + column
            b = row * segments + (column + 1) % segments
            c = (row + 1) * segments + (column + 1) % segments
            d = (row + 1) * segments + column
            if flip and (row + column) % 2:
                faces.extend(((a, b, d), (b, c, d)))
            else:
                faces.extend(((a, b, c), (a, c, d)))
    if reverse:
        faces = [tuple(reversed(face)) for face in faces]
    return MeshData(tuple(vertices), tuple(faces))


class TriangulatedLimbTests(unittest.TestCase):
    def test_two_different_triangulations_produce_same_regular_grid(self):
        for flipped in (False, True):
            with self.subTest(flipped=flipped):
                source = tube(flip=flipped)
                result = try_remesh_triangulated_limb(build_engine_input(
                    source, RemeshSettings(target_quad_count=240, topology_mode="STRUCTURED")
                ))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_count, 240)
                self.assertEqual(analysis.boundary_edge_count, 32)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertEqual(len(result.vertices), 256)
                self.assertTrue(all(len(face) == 4 for face in result.faces))

    def test_reverse_winding_is_preserved(self):
        source = tube(reverse=True)
        result = try_remesh_triangulated_limb(build_engine_input(
            source, RemeshSettings(target_quad_count=240)
        ))
        self.assertIsNotNone(result)
        assert result is not None
        a, b, c = (result.vertices[index] for index in result.faces[0][:3])
        ab = tuple(b[index] - a[index] for index in range(3))
        ac = tuple(c[index] - a[index] for index in range(3))
        normal = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
                  ab[0] * ac[1] - ab[1] * ac[0])
        radial = tuple(a[index] - (0.0, 0.0, a[2])[index] for index in range(3))
        self.assertLess(sum(normal[index] * radial[index] for index in range(3)), 0.0)

    def test_rigidly_rotated_and_translated_tube(self):
        source = tube(flip=True)
        angle = 0.7
        transform = lambda point: (
            2.0 + point[0] * math.cos(angle) + point[2] * math.sin(angle),
            -1.0 + point[1],
            0.4 - point[0] * math.sin(angle) + point[2] * math.cos(angle),
        )
        rotated = MeshData(tuple(transform(point) for point in source.vertices), source.faces)
        result = try_remesh_triangulated_limb(build_engine_input(
            rotated, RemeshSettings(target_quad_count=240)
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(analyze_mesh(result).quad_count, 240)

    def test_disconnected_or_branching_surface_is_rejected(self):
        source = tube()
        extra = MeshData(
            source.vertices + ((1.0, 0.0, 1.0),),
            source.faces + ((0, 1, len(source.vertices)),),
        )
        result = try_remesh_triangulated_limb(build_engine_input(
            extra, RemeshSettings(target_quad_count=240)
        ))
        self.assertIsNone(result)

    def test_unmatched_loop_is_rejected(self):
        source = tube()
        loop = tuple((5.0 + math.cos(2 * math.pi * index / 12),
                      math.sin(2 * math.pi * index / 12), 1.2) for index in range(12))
        guide = GuideCurveData("잘못된 가이드", (loop,), kind=("LOOP",), closed=(True,))
        result = try_remesh_triangulated_limb(build_engine_input(
            source, RemeshSettings(target_quad_count=240), guide_curves=(guide,)
        ))
        self.assertIsNone(result)

    def test_cancellation(self):
        source = tube()
        with self.assertRaises(RemeshCancelled):
            try_remesh_triangulated_limb(build_engine_input(
                source, RemeshSettings(target_quad_count=240)
            ), cancelled=lambda: True)

    def test_large_grid_reuses_source_ring_intersections(self):
        source = tube(segments=32, rows=48, flip=True)
        real_ray_polygon = triangulated_limb._ray_polygon
        with patch.object(triangulated_limb, "_ray_polygon", wraps=real_ray_polygon) as ray_polygon:
            result = try_remesh_triangulated_limb(build_engine_input(
                source, RemeshSettings(target_quad_count=5000)
            ))
        self.assertIsNotNone(result)
        assert result is not None
        output_segments = analyze_mesh(result).boundary_edge_count // 2
        self.assertLessEqual(ray_polygon.call_count, 48 * output_segments)
        self.assertLess(ray_polygon.call_count, len(result.vertices))

    def test_cancellation_during_ring_sampling(self):
        source = tube(segments=32, rows=48)
        ray_calls = 0
        real_ray_polygon = triangulated_limb._ray_polygon

        def tracked_ray(*args):
            nonlocal ray_calls
            ray_calls += 1
            return real_ray_polygon(*args)

        with patch.object(triangulated_limb, "_ray_polygon", side_effect=tracked_ray):
            with self.assertRaises(RemeshCancelled):
                try_remesh_triangulated_limb(build_engine_input(
                    source, RemeshSettings(target_quad_count=5000)
                ), cancelled=lambda: ray_calls >= 8)
        self.assertGreaterEqual(ray_calls, 8)
        self.assertLess(ray_calls, 32)


if __name__ == "__main__":
    unittest.main()
