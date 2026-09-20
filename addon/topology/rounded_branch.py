"""세 원형 링과 팔 방향선으로 제한된 둥근 T 분기 쿼드 차트를 만든다."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import isfinite, pi, sqrt

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3
from ..surface import SurfaceIndex
from .branch_t import _Shape, _closed_sphere, _make_cage
from .quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout


def try_remesh_rounded_branch(
    engine_input: EngineInput, *, cancelled: CancelledCallback | None = None
) -> MeshData | None:
    """축 정렬 원통형 몸통·팔 한 분기만 독립 쿼드 연결로 재구성한다."""
    mesh, settings = engine_input.mesh, engine_input.settings
    if (settings.symmetry_axes or mesh.hard_edges or settings.target_quad_count < 100
            or settings.target_quad_count > 5000 or abs(settings.density_scale - 1.0) > 1e-7
            or any(len(face) != 3 for face in mesh.faces)
            or (engine_input.density_values and max(engine_input.density_values) - min(engine_input.density_values) > 1e-7)):
        return None
    if not _closed_sphere(mesh):
        return None
    parsed = _parse_guides(engine_input)
    if parsed is None:
        return None
    loops, strip, shape, radius = parsed
    options: list[tuple[int, MeshData, float]] = []
    for divisions in range(3, 31):
        _cancel(cancelled)
        nominal_step = min(2 * radius, shape.az1 - shape.az0) / divisions
        candidate, step = _make_local_cage(shape, float(f"{nominal_step:.12g}"), cancelled)
        if candidate is not None:
            options.append((abs(len(candidate.faces) - settings.target_quad_count), candidate, step))
            if divisions >= 5 and len(candidate.faces) > settings.target_quad_count * 1.5:
                break
    if not options:
        return None
    _, cage, spacing = min(options, key=lambda item: item[0])
    if abs(len(cage.faces) - settings.target_quad_count) / settings.target_quad_count > .05:
        return None

    surface = SurfaceIndex(mesh)
    normals = _vertex_normals(cage)
    projected: list[Vector3] = []
    for index, point in enumerate(cage.vertices):
        _cancel(cancelled)
        try:
            near, distance, _ = surface.nearest(point, normal=normals[index], min_normal_dot=.2)
        except ValueError:
            return None
        # 둥근 끝의 사각 seed 모서리만 큰 이동을 허용한다.
        cap = (point[2] < shape.z0 + radius*.65 or point[2] > shape.z1 - radius*.65
               or point[0] > shape.ax1 - radius*.65)
        if distance > radius * (.80 if cap else .50):
            return None
        projected.append(near)
    output = MeshData(tuple(projected), cage.faces)
    if not _closed_sphere(output):
        return None
    if not _face_orientation_ok(output, surface, cancelled):
        return None
    if _self_intersects(output, cancelled):
        return None

    tolerance = spacing * .60
    layout = validate_layout(output, LayoutExpectations(
        loops=tuple(EdgePathExpectation(f"branch:{index}", points, True, tolerance) for index, points in enumerate(loops)),
        strips=(EdgePathExpectation("branch:strip", strip, False, tolerance),),
        boundary_edge_count=0, max_face_aspect_ratio=5.0,
    ))
    if not layout.ok or not _poles_ok(output, shape, loops, strip, spacing):
        return None
    distance = measure_bidirectional_sample_distance(mesh, output, cancelled=cancelled)
    if distance.max_distance > max(spacing * .5, radius * .06):
        return None
    return output


def _parse_guides(data: EngineInput):
    loops: list[tuple[Vector3, ...]] = []
    strips: list[tuple[Vector3, ...]] = []
    names = {guide.name for guide in data.guide_curves}
    if data.settings.guide_curve_names and not set(data.settings.guide_curve_names).issubset(names):
        return None
    for guide in data.guide_curves:
        for points, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            if any(not all(isfinite(v) for v in point) for point in points):
                return None
            if kind == "LOOP" and closed and len(points) >= 8:
                loops.append(tuple(points[:-1] if _dist(points[0], points[-1]) < 1e-6 else points))
            elif kind == "STRIP" and not closed and len(points) >= 2:
                strips.append(tuple(points))
            else:
                return None
    if len(loops) != 3 or len(strips) != 1:
        return None
    body = []
    arm = []
    for loop in loops:
        spans = [max(p[i] for p in loop) - min(p[i] for p in loop) for i in range(3)]
        if spans[2] < .005 * max(spans[0], spans[1]):
            body.append(loop)
        elif spans[0] < .005 * max(spans[1], spans[2]):
            arm.append(loop)
        else:
            return None
    if len(body) != 2 or len(arm) != 1:
        return None
    body.sort(key=lambda points: sum(p[2] for p in points) / len(points))
    c0, r0 = _circle(body[0], (0, 1), 2)
    c1, r1 = _circle(body[1], (0, 1), 2)
    ca, ra = _circle(arm[0], (1, 2), 0)
    if any(value is None for value in (c0, c1, ca)):
        return None
    assert c0 is not None and c1 is not None and ca is not None
    if abs(r0-r1) > r0*.03 or abs(c0[0]-c1[0]) > r0*.03 or abs(c0[1]-c1[1]) > r0*.03:
        return None
    radius = (r0+r1)/2
    if abs(ca[0]-c0[1]) > radius*.03 or not radius*.25 < ra < radius*.8:
        return None
    bounds = tuple((min(p[i] for p in data.mesh.vertices), max(p[i] for p in data.mesh.vertices)) for i in range(3))
    zlow, zhigh = c0[2], c1[2]
    if not (bounds[2][0] < zlow < ca[1]-ra < ca[1]+ra < zhigh < bounds[2][1]):
        return None
    if not (c0[0]+radius < ca[2] < bounds[0][1] and abs(bounds[0][0]-(c0[0]-radius)) < radius*.05):
        return None
    if abs(bounds[1][0]-(c0[1]-radius)) > radius*.05 or abs(bounds[1][1]-(c0[1]+radius)) > radius*.05:
        return None
    strip = strips[0]
    if (min(p[0] for p in strip) <= c0[0]+radius or max(p[0] for p in strip) > ca[2]+radius*.02
            or max(p[1] for p in strip)-min(p[1] for p in strip) > ra*.05
            or max(p[2] for p in strip)-min(p[2] for p in strip) > ra*.05
            or abs(sum(p[1] for p in strip)/len(strip)-ca[0]) > ra*.08
            or abs(sum(p[2] for p in strip)/len(strip)-(ca[1]+ra)) > ra*.08):
        return None
    snap = radius / 60
    end = lambda value: round(value / snap) * snap
    shape = _Shape(c0[0]-radius,c0[0]+radius,c0[1]-radius,c0[1]+radius,end(bounds[2][0]),end(bounds[2][1]),
                   ca[0]-ra,ca[0]+ra,ca[1]-ra,ca[1]+ra,end(bounds[0][1]),zlow,zhigh,ca[2])
    return (body[0], body[1], arm[0]), strip, shape, radius


def _circle(points, axes, fixed):
    center = tuple(sum(point[axis] for point in points)/len(points) for axis in axes)
    level = sum(point[fixed] for point in points)/len(points)
    radii = [sqrt(sum((point[axis]-center[i])**2 for i,axis in enumerate(axes))) for point in points]
    radius = sum(radii)/len(radii)
    if radius < 1e-6 or max(abs(r-radius) for r in radii) > radius*.03:
        return None, 0.0
    angles = [__import__('math').atan2(point[axes[1]]-center[1],point[axes[0]]-center[0]) for point in points]
    sweep = sum(((b-a+pi)%(2*pi)-pi) for a,b in zip(angles, angles[1:]+angles[:1]))
    if abs(abs(sweep)-2*pi) > .1:
        return None, 0.0
    if fixed == 2:
        return (center[0],center[1],level),radius
    return (center[0],center[1],level),radius


def _vertex_normals(mesh):
    normals = [[0.0,0.0,0.0] for _ in mesh.vertices]
    for face in mesh.faces:
        a,b,c = (mesh.vertices[i] for i in face[:3])
        normal = _cross(_sub(b,a),_sub(c,a))
        for index in face:
            for axis in range(3):
                normals[index][axis] += normal[axis]
    return tuple(_unit(tuple(normal)) for normal in normals)


def _make_local_cage(shape: _Shape, spacing: float, cancelled: CancelledCallback | None):
    """입력의 위치와 무관한 중심 좌표에서 정수 격자 분할을 계산한다."""
    origin = ((shape.bx0+shape.bx1)/2, (shape.by0+shape.by1)/2, (shape.z0+shape.z1)/2)
    def local(value: float, axis: int) -> float:
        return float(f"{value-origin[axis]:.12g}")
    local_shape = _Shape(
        local(shape.bx0,0),local(shape.bx1,0),local(shape.by0,1),local(shape.by1,1),
        local(shape.z0,2),local(shape.z1,2),local(shape.ay0,1),local(shape.ay1,1),
        local(shape.az0,2),local(shape.az1,2),local(shape.ax1,0),
        local(shape.lower_guide,2),local(shape.upper_guide,2),local(shape.arm_guide,0),
    )
    cage, actual_spacing = _make_cage(local_shape, spacing, cancelled)
    if cage is None:
        return None, actual_spacing
    vertices = tuple(tuple(point[axis]+origin[axis] for axis in range(3)) for point in cage.vertices)
    return MeshData(vertices,cage.faces),actual_spacing


def _face_orientation_ok(mesh, source, cancelled):
    for face in mesh.faces:
        _cancel(cancelled)
        points = [mesh.vertices[i] for i in face]
        center = tuple(sum(p[i] for p in points)/4 for i in range(3))
        normal = _unit(_cross(_sub(points[1],points[0]),_sub(points[2],points[0])))
        other = _unit(_cross(_sub(points[2],points[0]),_sub(points[3],points[0])))
        if _dot(normal,other) <= .2:
            return False
        try:
            _,_,index = source.nearest(center,normal=normal,min_normal_dot=.0)
        except ValueError:
            return False
        if _dot(normal,source.face_normals[index]) < .3:
            return False
    return True


def _poles_ok(mesh, shape, loops, strip, spacing):
    neighbors = defaultdict(set)
    for face in mesh.faces:
        for a,b in zip(face,face[1:]+face[:1]):
            neighbors[a].add(b)
            neighbors[b].add(a)
    poles = [(mesh.vertices[i],len(near)) for i,near in neighbors.items() if len(near) != 4]
    if Counter(valence for _,valence in poles) != Counter({3:12,5:4}):
        return False
    arm_radius = (shape.ay1-shape.ay0)/2
    for point,valence in poles:
        if any(_path_distance(point,guide,closed) < spacing*.35 for guide,closed in (*((guide,True) for guide in loops),(strip,False))):
            return False
        if valence == 5 and not (shape.bx1-arm_radius < point[0] < shape.bx1+arm_radius
                                and shape.az0-arm_radius < point[2] < shape.az1+arm_radius):
            return False
    return True


def _self_intersects(mesh, cancelled):
    """서로 다른 면의 비공면 교차와 공면 내부 겹침을 확인한다."""
    triangles = [(face_id, tri) for face_id,face in enumerate(mesh.faces)
                 for tri in ((face[0],face[1],face[2]),(face[0],face[2],face[3]))]
    lengths = [_dist(mesh.vertices[a],mesh.vertices[b]) for face in mesh.faces
               for a,b in zip(face,face[1:]+face[:1])]
    cell = max(sum(lengths)/len(lengths),1e-6)
    buckets = defaultdict(list)
    from math import floor
    for index,(_,tri) in enumerate(triangles):
        low = [floor(min(mesh.vertices[v][axis] for v in tri)/cell) for axis in range(3)]
        high = [floor(max(mesh.vertices[v][axis] for v in tri)/cell) for axis in range(3)]
        for i in range(low[0],high[0]+1):
            for j in range(low[1],high[1]+1):
                for k in range(low[2],high[2]+1):
                    buckets[i,j,k].append(index)
    pairs = set()
    for bucket in buckets.values():
        for index,a in enumerate(bucket):
            for b in bucket[index+1:]:
                if triangles[a][0] != triangles[b][0] and len(set(triangles[a][1]) & set(triangles[b][1])) < 2:
                    pairs.add((min(a,b),max(a,b)))
    if len(pairs) > len(triangles)*80:
        return True
    for a,b in pairs:
        _cancel(cancelled)
        first = tuple(mesh.vertices[i] for i in triangles[a][1])
        second = tuple(mesh.vertices[i] for i in triangles[b][1])
        if _triangles_intersect(first,second):
            return True
    return False


def _triangles_intersect(a,b):
    for first,second in ((a,b),(b,a)):
        for i in range(3):
            if _segment_triangle(first[i],first[(i+1)%3],second):
                return True
    # 평행한 두 면은 평면 분리 거리 후 2D 내부 겹침을 검사한다.
    na = _cross(_sub(a[1],a[0]),_sub(a[2],a[0]))
    nb = _cross(_sub(b[1],b[0]),_sub(b[2],b[0]))
    if _dot(na,na) < 1e-16 or _dot(nb,nb) < 1e-16:
        return True
    if _dot(_cross(na,nb),_cross(na,nb)) > 1e-10*_dot(na,na)*_dot(nb,nb):
        return False
    if max(abs(_dot(na,_sub(point,a[0])))/sqrt(_dot(na,na)) for point in b) > 1e-7:
        return False
    axes = [axis for axis in range(3) if axis != max(range(3),key=lambda i:abs(na[i]))]
    aa = [tuple(point[i] for i in axes) for point in a]
    bb = [tuple(point[i] for i in axes) for point in b]
    for first,second in ((aa,bb),(bb,aa)):
        if any(_in_triangle_2d(point,second) for point in first):
            return True
    return any(_segments_cross_2d(aa[i],aa[(i+1)%3],bb[j],bb[(j+1)%3]) for i in range(3) for j in range(3))


def _segment_triangle(a,b,tri):
    p,q,r=tri
    direction=_sub(b,a);e1=_sub(q,p);e2=_sub(r,p)
    pv=_cross(direction,e2);det=_dot(e1,pv)
    if abs(det)<1e-12:
        return False
    inv=1/det;s=_sub(a,p);u=_dot(s,pv)*inv
    if not 1e-7<u<1-1e-7:
        return False
    qv=_cross(s,e1);v=_dot(direction,qv)*inv
    if not (1e-7 < v and u+v < 1-1e-7):
        return False
    t=_dot(e2,qv)*inv
    return 1e-7<t<1-1e-7


def _in_triangle_2d(p,tri):
    a,b,c=tri
    d1=_turn(a,b,p);d2=_turn(b,c,p);d3=_turn(c,a,p)
    return min(d1,d2,d3)>1e-9 or max(d1,d2,d3)<-1e-9


def _segments_cross_2d(a,b,c,d):
    return _turn(a,b,c)*_turn(a,b,d)<-1e-12 and _turn(c,d,a)*_turn(c,d,b)<-1e-12


def _turn(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _path_distance(point,guide,closed):
    pairs=list(zip(guide,guide[1:]))
    if closed:pairs.append((guide[-1],guide[0]))
    return min(_segment_distance(point,a,b) for a,b in pairs)


def _segment_distance(p,a,b):
    ab=_sub(b,a);ap=_sub(p,a);length=_dot(ab,ab)
    t=max(0,min(1,_dot(ap,ab)/length)) if length else 0
    return _dist(p,tuple(a[i]+t*ab[i] for i in range(3)))


def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _sub(a,b):return tuple(a[i]-b[i] for i in range(3))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _dist(a,b):return sqrt(sum((a[i]-b[i])**2 for i in range(3)))
def _unit(a):
    length=sqrt(_dot(a,a))
    return tuple(x/length for x in a) if length>1e-12 else (0.0,0.0,0.0)
def _cancel(callback):
    if callback and callback():raise RemeshCancelled("둥근 분기 생성이 취소되었습니다.")
