"""방향장과 표면 투영의 수치 결과를 검사한다."""
import unittest
from addon.core import MeshData, RemeshCancelled
from addon.field import solve_field, alignment, optimize_quads, _valid_move
from addon.surface import SurfaceIndex


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
                (0.52,0.48,0.03),
            ),
            ((0,1,4,3),(1,2,4),(2,3,4)),
            frozenset({(0,1),(1,2),(2,3),(0,3)}),
        )

        after,_=optimize_quads(mesh,surface,iterations=1)

        self.assertLess(abs(after.vertices[4][2]), 1e-12)


def _grid(n):
    vertices=tuple((float(x),float(y),0.) for y in range(n+1) for x in range(n+1))
    faces=tuple((y*(n+1)+x,y*(n+1)+x+1,(y+1)*(n+1)+x+1,(y+1)*(n+1)+x) for y in range(n) for x in range(n))
    return MeshData(vertices,faces)
