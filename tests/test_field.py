"""방향장과 표면 투영의 수치 결과를 검사한다."""
import math
import unittest
from collections import defaultdict
from addon.core import MeshData, RemeshCancelled
from addon.field import solve_field, alignment, optimize_quads, _valid_move, _curvature_anchors, _frame, _normal
from addon.surface import SurfaceIndex, center, cross, dot, unit


class FieldTests(unittest.TestCase):
    def test_projection_rejects_quad_with_degenerate_dominant_projection(self):
        vertices=((0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.))
        self.assertFalse(_valid_move(vertices,((0,1,2,3),),(0,),2,(1.,1.,4.)))
        self.assertTrue(_valid_move(vertices,((0,1,2,3),),(0,),2,(1.,1.,.05)))

    def test_projection_does_not_jump_to_distant_normal_compatible_surface(self):
        from tests.test_engine import _cube_mesh
        from addon.engine import _triangulated
        surface=SurfaceIndex(_triangulated(_cube_mesh(),()))
        grid=_grid(2)
        vertices=tuple((p[0]*.2-.2,p[1]*.2-.2,-.95) for p in grid.vertices)
        result,_=optimize_quads(MeshData(vertices,grid.faces),surface,iterations=0)
        self.assertAlmostEqual(result.vertices[4][2],-.95)

    def test_triangle_projection_has_known_closest_point(self):
        mesh=MeshData(((0.,0.,0.),(1.,0.,0.),(0.,1.,0.)),((0,1,2),))
        surface=SurfaceIndex(mesh)
        p,d,i=surface.nearest((.2,.3,2.))
        for a,b in zip(p,(.2,.3,0.)):
            self.assertAlmostEqual(a,b)
        self.assertAlmostEqual(d,2.)
        self.assertEqual(i,0)
        self.assertEqual(surface.nearest((2.,0.,0.))[0],(1.,0.,0.))

    def test_surface_index_requires_triangle_faces(self):
        with self.assertRaisesRegex(ValueError, "삼각형"):
            SurfaceIndex(MeshData(((0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.)),((0,1,2,3),)))
        with self.assertRaisesRegex(ValueError, "최소 1개"):
            SurfaceIndex(MeshData(((0.,0.,0.),(1.,0.,0.),(0.,1.,0.)),()))

    def test_guide_direction_propagates_to_interior_faces(self):
        mesh=_grid(8)
        straight=solve_field(mesh,(((0.,4.,0.),(8.,4.,0.)),))
        diagonal=solve_field(mesh,(((0.,0.,0.),(8.,8.,0.)),))
        center_index=4*8+4
        self.assertGreater(alignment(mesh.vertices,mesh.faces[center_index],straight[center_index]),.9)
        self.assertLess(alignment(mesh.vertices,mesh.faces[center_index],diagonal[center_index]),.35)

    def test_field_cancel(self):
        with self.assertRaises(RemeshCancelled):
            solve_field(_grid(3),cancelled=lambda:True)

    def test_alignment_optimization_improves_skew_grid(self):
        mesh=_grid(4)
        vertices=list(mesh.vertices)
        vertices[12]=(2.35,2.15,0.)
        skew=MeshData(tuple(vertices),mesh.faces)
        triangles=tuple(t for a,b,c,d in mesh.faces for t in ((a,b,c),(a,c,d)))
        surface=SurfaceIndex(MeshData(mesh.vertices,triangles))
        before=sum(alignment(skew.vertices,f,(1.,0.,0.)) for f in mesh.faces)/len(mesh.faces)
        after,_=optimize_quads(skew,surface,iterations=6)
        score=sum(alignment(after.vertices,f,(1.,0.,0.)) for f in mesh.faces)/len(mesh.faces)
        self.assertGreater(score,before)
        self.assertEqual(after.faces,skew.faces)
        self.assertTrue(all(abs(p[2])<1e-12 for p in after.vertices))

    def test_projection_cannot_shrink_a_face_below_engine_area_limit(self):
        from addon.engine import FACE_NORMAL_EPSILON, _validate_topology
        height = FACE_NORMAL_EPSILON * 2
        vertices = ((0.,0.,0.), (1.,0.,0.), (0.,height,0.))
        faces = ((0,1,2),)
        _validate_topology(MeshData(vertices, faces), 180.)
        self.assertFalse(_valid_move(vertices, faces, (0,), 2, (0.,height*.15,0.)))

    def test_fixed_patch_preserves_positions_when_surface_votes_tie(self):
        reference = MeshData(
            ((0.,0.,0.), (1.,0.,0.), (1.,1.,0.), (0.,1.,0.),
             (0.,0.,.05), (1.,0.,.05), (1.,1.,.05), (0.,1.,.05)),
            ((0,1,2), (0,2,3), (4,5,6), (4,6,7)),
        )
        mesh = MeshData(
            ((0.,0.,0.), (1.,0.,0.), (1.,1.,.05), (0.,1.,.05)),
            ((0,1,2,3),),
        )
        after, _ = optimize_quads(mesh, SurfaceIndex(reference), iterations=1)
        self.assertEqual(after, mesh)

    def test_curvature_anchor_aligns_field_to_cylinder_axis(self):
        for twist in (0.,.5):
            mesh=_cylinder(twist=twist)
            scores=_cylinder_alignment(mesh,solve_field(mesh))
            self.assertTrue(scores)
            self.assertGreater(min(scores),.97,f"twist={twist}")

    def test_curvature_anchor_is_weak_on_sphere(self):
        weights,_=_anchor_weights(_lat_lon_sphere())
        self.assertLess(max(weights),.3)
        cylinder=_cylinder()
        weights,centers=_anchor_weights(cylinder)
        middle=[w for w,p in zip(weights,centers) if 15.<=p[2]<=25.]
        self.assertTrue(middle)
        self.assertGreater(min(middle),.5)

    def test_projection_stays_on_mapped_component_for_close_parallel_sheets(self):
        reference=MeshData(
            (
                (0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.),
                (0.,0.,0.05),(1.,0.,0.05),(1.,1.,0.05),(0.,1.,0.05),
            ),
            ((0,1,2),(0,2,3),(4,6,5),(4,7,6)),
        )
        surface=SurfaceIndex(reference)
        mesh=MeshData(
            (
                (0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.),
                # v4 가 (0.52,0.48) 이면 z=0 으로 투영한 사변형의 1-4-3 이 일직선이 되어 퇴화한다.
                (0.55,0.48,0.03),
            ),
            ((0,1,4,3),(1,2,4),(2,3,4)),
            frozenset({(0,1),(1,2),(2,3),(0,3)}),
        )

        after,_=optimize_quads(mesh,surface,iterations=1)

        self.assertLess(abs(after.vertices[4][2]), 1e-12)


def _cylinder(radius=1., height=40., segments=24, rings=80, twist=0.):
    """열린 긴 원통. 사각 셀마다 같은 방향 대각선으로 삼각화한다."""
    vertices=[]
    for r in range(rings+1):
        z=height*r/rings
        offset=2*math.pi*twist*r/segments
        for s in range(segments):
            angle=2*math.pi*s/segments+offset
            vertices.append((radius*math.cos(angle),radius*math.sin(angle),z))
    faces=[]
    for r in range(rings):
        for s in range(segments):
            n=(s+1)%segments
            a,b=r*segments+s,r*segments+n
            c,d=(r+1)*segments+s,(r+1)*segments+n
            faces.append((a,b,d))
            faces.append((a,d,c))
    return MeshData(tuple(vertices),tuple(faces))


def _lat_lon_sphere(segments=20, rings=10):
    vertices=[(0.,0.,1.)]
    for j in range(1,rings):
        phi=math.pi*j/rings
        for i in range(segments):
            theta=2*math.pi*i/segments
            vertices.append((math.sin(phi)*math.cos(theta),math.sin(phi)*math.sin(theta),math.cos(phi)))
    vertices.append((0.,0.,-1.))
    south=len(vertices)-1
    faces=[(0,1+(i+1)%segments,1+i) for i in range(segments)]
    for j in range(rings-2):
        for i in range(segments):
            a=1+j*segments+i
            b=1+j*segments+(i+1)%segments
            c=1+(j+1)*segments+i
            d=1+(j+1)*segments+(i+1)%segments
            faces.append((a,b,d))
            faces.append((a,d,c))
    last=1+(rings-2)*segments
    faces.extend((south,last+i,last+(i+1)%segments) for i in range(segments))
    return MeshData(tuple(vertices),tuple(faces))


def _anchor_weights(mesh):
    normals=[_normal(mesh.vertices,f) for f in mesh.faces]
    frames=[_frame(n) for n in normals]
    centers=[center([mesh.vertices[v] for v in f]) for f in mesh.faces]
    edge_faces=defaultdict(list)
    for i,f in enumerate(mesh.faces):
        for a,b in zip(f,(*f[1:],f[0])):
            edge_faces[tuple(sorted((a,b)))].append(i)
    return _curvature_anchors(mesh.vertices,mesh.faces,normals,frames,centers,edge_faces,mesh.hard_edges)[1],centers


def _cylinder_alignment(mesh, directions, low=15., high=25.):
    """4방향장이므로 축과 둘레 방향 중 더 잘 맞는 쪽을 점수로 쓴다."""
    scores=[]
    for i,f in enumerate(mesh.faces):
        p=center([mesh.vertices[v] for v in f])
        if not low<=p[2]<=high:
            continue
        axis=(0.,0.,1.)
        tangent=cross(axis,unit((p[0],p[1],0.)))
        scores.append(max(abs(dot(directions[i],axis)),abs(dot(directions[i],tangent))))
    return scores


def _grid(n):
    vertices=tuple((float(x),float(y),0.) for y in range(n+1) for x in range(n+1))
    faces=tuple((y*(n+1)+x,y*(n+1)+x+1,(y+1)*(n+1)+x+1,(y+1)*(n+1)+x) for y in range(n) for x in range(n))
    return MeshData(vertices,faces)
