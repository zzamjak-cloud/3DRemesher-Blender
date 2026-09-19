"""독립 구조화 컴포넌트와 가이드 대응을 검증한다."""

from __future__ import annotations

import math
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.components import try_remesh_components


def _cube(offset: float = 0.0) -> MeshData:
    vertices = (
        (offset, 0, 0), (offset + 1, 0, 0), (offset + 1, 1, 0), (offset, 1, 0),
        (offset, 0, 1), (offset + 1, 0, 1), (offset + 1, 1, 1), (offset, 1, 1),
    )
    faces = ((0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7))
    return MeshData(vertices, faces)


def _tube(offset: float = 0.0) -> MeshData:
    segments, rows = 12, 9
    vertices = tuple(
        (offset + 0.35 * math.cos(2 * math.pi * segment / segments),
         0.35 * math.sin(2 * math.pi * segment / segments), row * 0.3)
        for row in range(rows) for segment in range(segments)
    )
    faces = []
    for row in range(rows - 1):
        for segment in range(segments):
            a = row * segments + segment
            b = row * segments + (segment + 1) % segments
            c = (row + 1) * segments + (segment + 1) % segments
            d = (row + 1) * segments + segment
            faces.extend(((a, b, c), (a, c, d)))
    return MeshData(vertices, tuple(faces))


def _join(*meshes: MeshData) -> MeshData:
    vertices, faces = [], []
    for mesh in meshes:
        offset = len(vertices)
        vertices.extend(mesh.vertices)
        faces.extend(tuple(index + offset for index in face) for face in mesh.faces)
    return MeshData(tuple(vertices), tuple(faces))


class ComponentTopologyTests(unittest.TestCase):
    def test_engine_routes_separate_components_without_legacy(self):
        source = _join(_tube(-4.0), _cube(3.0))
        result = RemeshBackend().remesh(build_engine_input(
            source, RemeshSettings(target_quad_count=768, topology_mode="STRUCTURED")
        ))
        self.assertEqual(len(_components(result.mesh)), 2)
        self.assertLess(abs(result.quality.actual_quad_count - 768) / 768, 0.05)
        self.assertFalse(any("실험 엔진" in warning for warning in result.warnings))

    def test_separate_triangulated_tube_and_cube(self):
        source = _join(_tube(-4.0), _cube(3.0))
        result = try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768, topology_mode="STRUCTURED")
        ))
        self.assertIsNotNone(result)
        assert result is not None
        quality = analyze_mesh(result)
        self.assertEqual(quality.quad_ratio, 1.0)
        self.assertEqual(quality.non_manifold_edge_count, 0)
        self.assertGreater(quality.boundary_edge_count, 0)
        self.assertLess(abs(quality.quad_count - 768) / 768, 0.05)
        self.assertEqual(len(_components(result)), 2)
        self.assertTrue(all(max(result.vertices[index][0] for index in group) < 0 or
                            min(result.vertices[index][0] for index in group) > 0
                            for group in _components(result)))

    def test_touching_cubes_keep_distinct_vertex_indices(self):
        source = _join(_cube(), _cube(1.0))
        result = try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768, topology_mode="STRUCTURED")
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(_components(result)), 2)
        self.assertEqual(analyze_mesh(result).boundary_edge_count, 0)
        touching = [index for index, point in enumerate(result.vertices) if abs(point[0] - 1.0) < 1.0e-9]
        self.assertGreater(len(touching), len({result.vertices[index] for index in touching}))

    def test_fully_overlapping_cubes_are_not_welded(self):
        source = _join(_cube(), _cube())
        result = try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768, topology_mode="STRUCTURED")
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(_components(result)), 2)
        self.assertEqual(analyze_mesh(result).boundary_edge_count, 0)
        self.assertEqual(len(result.vertices), 2 * len({point for point in result.vertices}))

    def test_guide_on_shared_face_is_ambiguous(self):
        source = _join(_cube(), _cube(1.0))
        guide = GuideCurveData(
            "접촉면", (((1.0, 0.2, 0.2), (1.0, 0.8, 0.2),
                      (1.0, 0.8, 0.8), (1.0, 0.2, 0.8)),),
            kind=("LOOP",), closed=(True,),
        )
        self.assertIsNone(try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768), (guide,)
        )))

    def test_tube_guide_is_routed_to_tube_component(self):
        source = _join(_tube(-4.0), _cube(3.0))
        loop = tuple((-4.0 + 0.35 * math.cos(2 * math.pi * segment / 12),
                      0.35 * math.sin(2 * math.pi * segment / 12), 1.2)
                     for segment in range(12))
        guide = GuideCurveData("팔꿈치", (loop,), kind=("LOOP",), closed=(True,))
        result = try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768), (guide,)
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(_components(result)), 2)
        tube_group = next(group for group in _components(result)
                          if max(result.vertices[index][0] for index in group) < 0)
        guided_row = [index for index in tube_group
                      if abs(result.vertices[index][2] - 1.2) < 1.0e-8]
        self.assertGreaterEqual(len(guided_row), 12)

    def test_spline_crossing_components_is_rejected(self):
        source = _join(_tube(-4.0), _cube(3.0))
        guide = GuideCurveData("불일치", (((-3.65, 0, 1.2), (3.0, 0.5, 0.5)),),
                               kind=("STRIP",), closed=(False,))
        self.assertIsNone(try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=768), (guide,)
        )))

    def test_unsupported_component_rejects_whole_result(self):
        plane = MeshData(((5, 0, 0), (6, 0, 0), (6, 1, 0), (5, 1, 0)), ((0, 1, 2, 3),))
        source = _join(_cube(), plane)
        self.assertIsNone(try_remesh_components(build_engine_input(
            source, RemeshSettings(target_quad_count=400)
        )))


def _components(mesh: MeshData) -> tuple[frozenset[int], ...]:
    neighbors: dict[int, set[int]] = {index: set() for index in range(len(mesh.vertices))}
    for face in mesh.faces:
        for index in face:
            neighbors[index].update(face)
    pending = set(neighbors)
    groups = []
    while pending:
        stack = [pending.pop()]
        group = set(stack)
        while stack:
            for adjacent in neighbors[stack.pop()] & pending:
                pending.remove(adjacent)
                group.add(adjacent)
                stack.append(adjacent)
        groups.append(frozenset(group))
    return tuple(groups)


if __name__ == "__main__":
    unittest.main()
