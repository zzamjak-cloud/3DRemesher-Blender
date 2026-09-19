"""연결된 T형 몸통·팔에서 입력 삼각 연결과 무관한 폐루프를 검사한다."""

from __future__ import annotations

import unittest

from addon.core import GuideCurveData, MeshData, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.branch_t import _Shape, _make_cage, try_remesh_branch_t
from addon.topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout


def _triangle_t(divisions: int, *, alternate: bool) -> MeshData:
    """두 상자 부피의 표면을 서로 다른 간격과 대각선으로 삼각화한다."""
    step = 0.3 / divisions
    xs = tuple(-0.6 + step * i for i in range(9 * divisions + 1))
    ys = tuple(-0.6 + step * i for i in range(4 * divisions + 1))
    zs = tuple(-1.5 + step * i for i in range(10 * divisions + 1))
    occupied = set()
    for i in range(len(xs) - 1):
        x = (xs[i] + xs[i+1]) / 2
        for j in range(len(ys) - 1):
            y = (ys[j] + ys[j+1]) / 2
            for k in range(len(zs) - 1):
                z = (zs[k] + zs[k+1]) / 2
                if x < 0.6 or (-0.3 < y < 0.3 and 0.0 < z < 0.6):
                    occupied.add((i, j, k))
    vertex_map: dict[tuple[int,int,int], int] = {}
    points = []
    faces = []
    def index(key):
        if key not in vertex_map:
            vertex_map[key] = len(points)
            points.append((xs[key[0]], ys[key[1]], zs[key[2]]))
        return vertex_map[key]
    for i, j, k in sorted(occupied):
        panels = (
            ((i+1,j,k), ((i+1,j,k),(i+1,j+1,k),(i+1,j+1,k+1),(i+1,j,k+1))),
            ((i-1,j,k), ((i,j,k+1),(i,j+1,k+1),(i,j+1,k),(i,j,k))),
            ((i,j+1,k), ((i,j+1,k),(i,j+1,k+1),(i+1,j+1,k+1),(i+1,j+1,k))),
            ((i,j-1,k), ((i+1,j,k),(i+1,j,k+1),(i,j,k+1),(i,j,k))),
            ((i,j,k+1), ((i,j,k+1),(i+1,j,k+1),(i+1,j+1,k+1),(i,j+1,k+1))),
            ((i,j,k-1), ((i,j+1,k),(i+1,j+1,k),(i+1,j,k),(i,j,k))),
        )
        for neighbor, panel in panels:
            if neighbor in occupied:
                continue
            a,b,c,d = (index(key) for key in panel)
            if alternate and (i+j+k) % 2:
                faces.extend(((a,b,d),(b,c,d)))
            else:
                faces.extend(((a,b,c),(a,c,d)))
    return MeshData(tuple(points), tuple(faces))


def _guides() -> tuple[GuideCurveData, ...]:
    def loop(name, points):
        return GuideCurveData(name, (tuple(points),), kind=("LOOP",), closed=(True,))
    return (
        loop("REMESH_GUIDE_LOOP_body_lower", ((-0.6,-0.6,-1.2),(0.6,-0.6,-1.2),
                                                (0.6,0.6,-1.2),(-0.6,0.6,-1.2))),
        loop("REMESH_GUIDE_LOOP_body_upper", ((-0.6,-0.6,1.2),(0.6,-0.6,1.2),
                                                (0.6,0.6,1.2),(-0.6,0.6,1.2))),
        loop("REMESH_GUIDE_LOOP_arm", ((1.8,-0.3,0.0),(1.8,0.3,0.0),
                                       (1.8,0.3,0.6),(1.8,-0.3,0.6))),
    )


class BranchTTests(unittest.TestCase):
    def test_extreme_axis_ratio_rejects_before_allocating_grid(self):
        shape = _Shape(-0.6, 0.6, -0.6, 0.6, -1_500_000.0, 1_500_000.0,
                       -0.3, 0.3, 0.0, 0.6, 2.1,
                       -1_200_000.0, 1_200_000.0, 1.8)
        candidate, _ = _make_cage(shape, 0.3, None)
        self.assertIsNone(candidate)

    def test_two_unrelated_triangle_grids_produce_new_connected_loop_cage(self):
        guides = _guides()
        for divisions, alternate in ((3, False), (4, True)):
            with self.subTest(divisions=divisions):
                source = _triangle_t(divisions, alternate=alternate)
                result = try_remesh_branch_t(build_engine_input(
                    source, RemeshSettings(target_quad_count=900, topology_mode="STRUCTURED"), guides,
                ))
                self.assertIsNotNone(result)
                assert result is not None
                analysis = analyze_mesh(result)
                self.assertEqual(analysis.quad_ratio, 1.0)
                self.assertEqual(analysis.boundary_edge_count, 0)
                self.assertEqual(analysis.non_manifold_edge_count, 0)
                self.assertLessEqual(abs(analysis.quad_count - 900) / 900, 0.05)
                self.assertNotEqual(set(source.vertices), set(result.vertices))
                neighbors = {index: set() for index in range(len(result.vertices))}
                for face in result.faces:
                    for first, second in zip(face, (*face[1:], face[0])):
                        neighbors[first].add(second)
                        neighbors[second].add(first)
                guide_points = {point for guide in guides for point in guide.splines[0]}
                for index, point in enumerate(result.vertices):
                    if point in guide_points:
                        self.assertEqual(len(neighbors[index]), 4)
                self.assertTrue(any(len(nearby) != 4 for nearby in neighbors.values()))
                report = validate_layout(result, LayoutExpectations(
                    loops=tuple(EdgePathExpectation(g.name, g.splines[0], closed=True, tolerance=0.04)
                                for g in guides),
                    boundary_edge_count=0, max_face_aspect_ratio=5.0,
                ))
                self.assertTrue(report.ok, report.issues)
                self.assertLess(measure_bidirectional_sample_distance(source, result).max_distance, 1.0e-5)

    def test_missing_guide_rejects(self):
        source = _triangle_t(2, alternate=False)
        self.assertIsNone(try_remesh_branch_t(build_engine_input(
            source, RemeshSettings(target_quad_count=900, topology_mode="STRUCTURED"), _guides()[:2],
        )))

    def test_higher_quad_budget_keeps_three_branch_loops(self):
        guides = _guides()
        source = _triangle_t(3, alternate=True)
        result = try_remesh_branch_t(build_engine_input(
            source, RemeshSettings(target_quad_count=3000, topology_mode="STRUCTURED"), guides,
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertLessEqual(abs(len(result.faces) - 3000) / 3000, 0.05)
        self.assertTrue(validate_layout(result, LayoutExpectations(
            loops=tuple(EdgePathExpectation(g.name, g.splines[0], closed=True, tolerance=0.025)
                        for g in guides),
            boundary_edge_count=0, max_face_aspect_ratio=5.0,
        )).ok)

    def test_unrelated_closed_surface_rejects(self):
        source = _triangle_t(2, alternate=False)
        changed = list(source.vertices)
        changed[0] = (changed[0][0] + 0.03, changed[0][1] + 0.03, changed[0][2])
        self.assertIsNone(try_remesh_branch_t(build_engine_input(
            MeshData(tuple(changed), source.faces), RemeshSettings(target_quad_count=900, topology_mode="STRUCTURED"),
            _guides(),
        )))

    def test_crossed_guide_rejects(self):
        guides = list(_guides())
        points = list(guides[0].splines[0])
        points[1], points[2] = points[2], points[1]
        guides[0] = GuideCurveData(guides[0].name, (tuple(points),), kind=("LOOP",), closed=(True,))
        self.assertIsNone(try_remesh_branch_t(build_engine_input(
            _triangle_t(2, alternate=False), RemeshSettings(target_quad_count=900, topology_mode="STRUCTURED"), guides,
        )))


if __name__ == "__main__":
    unittest.main()
