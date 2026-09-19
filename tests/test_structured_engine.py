from __future__ import annotations

import math
import unittest
from collections import Counter

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, build_engine_input


def _cube(*, triangulated: bool = False, angle: float = 0.0) -> MeshData:
    original = (
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
    )
    cosine, sine = math.cos(angle), math.sin(angle)
    vertices = tuple((cosine * x - sine * y, sine * x + cosine * y, z) for x, y, z in original)
    quads = (
        (0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
        (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
    )
    faces = tuple(face for quad in quads for face in ((quad[0], quad[1], quad[2]), (quad[0], quad[2], quad[3]))) if triangulated else quads
    return MeshData(vertices, faces)


class StructuredEngineTests(unittest.TestCase):
    def test_cube_has_six_uniform_grids_without_extra_poles(self):
        source = _cube()
        result = RemeshBackend().remesh(build_engine_input(source, RemeshSettings(target_quad_count=384)))
        mesh = result.mesh
        self.assertEqual(len(mesh.faces), 384)
        self.assertEqual(result.quality.boundary_edge_count, 0)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)
        self.assertEqual(result.quality.degenerate_face_count, 0)
        self.assertFalse(any("실험 엔진" in warning for warning in result.warnings))
        valence = Counter()
        for face in mesh.faces:
            self.assertEqual(len(face), 4)
            for index, vertex in enumerate(face):
                valence[tuple(sorted((vertex, face[(index + 1) % 4])))] += 1
        degrees = Counter()
        for first, second in valence:
            degrees[first] += 1
            degrees[second] += 1
        self.assertEqual(Counter(degrees.values()), {4: len(mesh.vertices) - 8, 3: 8})
        self.assertTrue(all(count == 2 for count in valence.values()))
        for axis in range(3):
            for side in (-1.0, 1.0):
                face_count = sum(
                    all(abs(mesh.vertices[index][axis] - side) < 1e-6 for index in face)
                    for face in mesh.faces
                )
                self.assertEqual(face_count, 64)

    def test_triangulation_and_rotation_keep_cube_grid(self):
        source = _cube(triangulated=True, angle=0.37)
        result = RemeshBackend().remesh(build_engine_input(source, RemeshSettings(target_quad_count=384, topology_mode="STRUCTURED")))
        self.assertEqual(result.quality.actual_quad_count, 384)
        self.assertEqual(result.quality.non_manifold_edge_count, 0)

    def test_required_loop_is_rejected_when_layout_cannot_preserve_it(self):
        guide = GuideCurveData(
            name="REMESH_GUIDE_LOOP_eye",
            splines=(((0.0, 0.0, 1.0), (0.2, 0.0, 1.0), (0.0, 0.2, 1.0)),),
            kind=("LOOP",),
            closed=(True,),
        )
        engine_input = build_engine_input(
            _cube(), RemeshSettings(target_quad_count=384), guide_curves=(guide,)
        )
        with self.assertRaisesRegex(ValueError, "필수 가이드"):
            RemeshBackend().remesh(engine_input)

    def test_tube_loop_guide_becomes_closed_output_ring(self):
        segments = 16
        heights = (0.0, 0.3, 1.7, 3.0)
        vertices = tuple(
            (math.cos(2 * math.pi * segment / segments), math.sin(2 * math.pi * segment / segments), height)
            for height in heights for segment in range(segments)
        )
        faces = tuple(
            (row * segments + segment, row * segments + (segment + 1) % segments,
             (row + 1) * segments + (segment + 1) % segments, (row + 1) * segments + segment)
            for row in range(len(heights) - 1) for segment in range(segments)
        )
        guide = GuideCurveData(
            name="REMESH_GUIDE_LOOP_limb",
            splines=(tuple(
                (math.cos(2 * math.pi * index / 32), math.sin(2 * math.pi * index / 32), 1.4)
                for index in range(32)
            ),),
            kind=("LOOP",), closed=(True,),
        )
        result = RemeshBackend().remesh(build_engine_input(
            MeshData(vertices, faces), RemeshSettings(target_quad_count=128), guide_curves=(guide,)
        ))
        ring = [index for index, point in enumerate(result.mesh.vertices) if abs(point[2] - 1.4) < 1e-8]
        self.assertEqual(len(ring), segments)
        edges = {
            tuple(sorted((face[index], face[(index + 1) % 4])))
            for face in result.mesh.faces for index in range(4)
        }
        ring_edges = {edge for edge in edges if edge[0] in ring and edge[1] in ring}
        self.assertEqual(len(ring_edges), segments)
        self.assertEqual(result.quality.actual_quad_count, 128)


if __name__ == "__main__":
    unittest.main()
