"""독립 삼각 표면에서 원통형 T 분기 쿼드 경로를 검증한다."""

from __future__ import annotations

from math import cos, pi, sin, sqrt
import unittest

from addon.core import GuideCurveData, MeshData, RemeshBackend, RemeshSettings, analyze_mesh, build_engine_input
from addon.topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout
from addon.topology.rounded_branch import try_remesh_rounded_branch


def _capsule(point, first, second, radius):
    axis = tuple(second[i] - first[i] for i in range(3))
    relative = tuple(point[i] - first[i] for i in range(3))
    fraction = max(0.0, min(1.0, sum(relative[i] * axis[i] for i in range(3)) / sum(x*x for x in axis)))
    return sqrt(sum((relative[i] - fraction*axis[i])**2 for i in range(3))) - radius


def _field(point):
    body = _capsule(point, (0, 0, -1.4), (0, 0, 1.4), .6)
    arm = _capsule(point, (.45, 0, .25), (1.65, 0, .25), .32)
    smooth = .16
    blend = max(smooth - abs(body - arm), 0.0) / smooth
    return min(body, arm) - blend*blend*smooth*.25


def _cross(a,b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def make_rounded_branch_source(step: float = .13) -> MeshData:
    """캡슐 합집합의 암시적 표면을 마칭 사면체로 독립 삼각화한다."""
    xs = [-1.0 + i*step for i in range(round(3.12/step)+1)]
    ys = [-1.0 + i*step for i in range(round(1.95/step)+1)]
    zs = [-2.1 + i*step for i in range(round(4.42/step)+1)]
    ni,nj,nk = len(xs),len(ys),len(zs)
    grid_index = lambda i,j,k: (i*nj+j)*nk+k
    grid_points = [(x,y,z) for x in xs for y in ys for z in zs]
    values = [_field(point) for point in grid_points]
    corners = ((0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1))
    tetrahedra = ((0,5,1,6),(0,1,2,6),(0,2,3,6),(0,3,7,6),(0,7,4,6),(0,4,5,6))
    vertices = []
    edge_points = {}
    triangles = []

    def intersection(a,b):
        key = (min(a,b),max(a,b))
        if key in edge_points:
            return edge_points[key]
        fraction = values[a]/(values[a]-values[b])
        start,end = grid_points[a],grid_points[b]
        index = len(vertices)
        vertices.append(tuple(start[c]+fraction*(end[c]-start[c]) for c in range(3)))
        edge_points[key] = index
        return index

    for i in range(ni-1):
        for j in range(nj-1):
            for k in range(nk-1):
                cube = [grid_index(i+di,j+dj,k+dk) for di,dj,dk in corners]
                for tetrahedron in tetrahedra:
                    inside = [cube[c] for c in tetrahedron if values[cube[c]] < 0]
                    outside = [cube[c] for c in tetrahedron if values[cube[c]] >= 0]
                    if len(inside) == 1:
                        triangles.append(tuple(intersection(inside[0],b) for b in outside))
                    elif len(inside) == 3:
                        triangles.append(tuple(intersection(a,outside[0]) for a in inside))
                    elif len(inside) == 2:
                        a,b = inside
                        c,d = outside
                        p,q,r,s = intersection(a,c),intersection(a,d),intersection(b,c),intersection(b,d)
                        triangles.extend(((p,q,r),(q,s,r)))

    # SDF 기울기로 모든 삼각면의 바깥 방향을 맞춘다.
    for index,face in enumerate(triangles):
        p,q,r = (vertices[v] for v in face)
        normal = _cross(tuple(q[c]-p[c] for c in range(3)),tuple(r[c]-p[c] for c in range(3)))
        midpoint = tuple((p[c]+q[c]+r[c])/3 for c in range(3))
        epsilon = .001
        gradient = tuple(
            (_field(tuple(midpoint[c]+(epsilon if c==axis else 0) for c in range(3)))
             - _field(tuple(midpoint[c]-(epsilon if c==axis else 0) for c in range(3))))/(2*epsilon)
            for axis in range(3)
        )
        if sum(normal[c]*gradient[c] for c in range(3)) < 0:
            triangles[index] = (face[0],face[2],face[1])
    return MeshData(tuple(vertices),tuple(triangles))


def make_rounded_branch_guides() -> tuple[GuideCurveData, ...]:
    """몸통 폐루프 둘, 팔 폐루프 하나와 팔 상단 열린 방향선을 만든다."""
    def loop(name,points):
        return GuideCurveData(name,(points,),kind=("LOOP",),closed=(True,))
    body = lambda z: tuple((.6*cos(2*pi*i/32),.6*sin(2*pi*i/32),z) for i in range(32))
    arm = tuple((1.4,.32*sin(2*pi*i/32),.25+.32*cos(2*pi*i/32)) for i in range(32))
    strip = tuple((.92+.48*i/16,0,.57) for i in range(17))
    return (
        loop("body_lower",body(-1.2)),loop("body_upper",body(1.2)),loop("arm_ring",arm),
        GuideCurveData("arm_strip",(strip,),kind=("STRIP",),closed=(False,)),
    )


class RoundedBranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = make_rounded_branch_source()
        cls.guides = make_rounded_branch_guides()

    def test_independent_triangulation_has_one_closed_component(self):
        source = self.source
        analysis = analyze_mesh(source)
        self.assertEqual(analysis.boundary_edge_count,0)
        self.assertEqual(analysis.non_manifold_edge_count,0)
        self.assertEqual(len(source.vertices)-analysis.edge_count+len(source.faces),2)
        self.assertEqual(validate_layout(source).metrics.bowtie_vertex_count,0)

    def test_four_guides_form_new_quad_paths(self):
        source = self.source
        result = try_remesh_rounded_branch(build_engine_input(
            source,RemeshSettings(target_quad_count=1072,topology_mode="STRUCTURED"),self.guides,
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.faces),1072)
        self.assertTrue(all(len(face)==4 for face in result.faces))
        self.assertNotEqual(set(source.vertices),set(result.vertices))
        report = validate_layout(result,LayoutExpectations(
            loops=tuple(EdgePathExpectation(g.name,g.splines[0],True,.096) for g in self.guides[:3]),
            strips=(EdgePathExpectation("arm_strip",self.guides[3].splines[0],False,.096),),
            boundary_edge_count=0,max_face_aspect_ratio=5.0,
        ))
        self.assertTrue(report.ok,report.issues)
        self.assertLess(measure_bidirectional_sample_distance(source,result).max_distance,.08)

    def test_missing_strip_rejects(self):
        self.assertIsNone(try_remesh_rounded_branch(build_engine_input(
            self.source,RemeshSettings(target_quad_count=1072,topology_mode="STRUCTURED"),self.guides[:3],
        )))

    def test_crossed_loop_rejects(self):
        guides = list(self.guides)
        points = list(guides[0].splines[0])
        points[8],points[16] = points[16],points[8]
        guides[0] = GuideCurveData(guides[0].name,(tuple(points),),kind=("LOOP",),closed=(True,))
        self.assertIsNone(try_remesh_rounded_branch(build_engine_input(
            self.source,RemeshSettings(target_quad_count=1072,topology_mode="STRUCTURED"),guides,
        )))

    def test_translation_preserves_branch_and_engine_route(self):
        for shift in ((0.0,-10.0,0.0),(8.0,-10.0,5.0)):
            with self.subTest(shift=shift):
                def move(point):
                    return tuple(point[axis]+shift[axis] for axis in range(3))
                source = MeshData(tuple(move(point) for point in self.source.vertices),self.source.faces)
                guides = tuple(GuideCurveData(
                    guide.name,
                    tuple(tuple(move(point) for point in spline) for spline in guide.splines),
                    kind=guide.kind,closed=guide.closed,
                ) for guide in self.guides)
                data = build_engine_input(source,RemeshSettings(
                    target_quad_count=1072,topology_mode="STRUCTURED"),guides)
                result = try_remesh_rounded_branch(data)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(len(result.faces),1072)
                if shift[0] != 0.0:
                    routed = RemeshBackend().remesh(data)
                    self.assertEqual(routed.quality.actual_quad_count,1072)
                    self.assertEqual(routed.quality.quad_ratio,1.0)


if __name__ == "__main__":
    unittest.main()
