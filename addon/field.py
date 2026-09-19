"""특징선·곡률·가이드 제약을 이웃 표면으로 전파하는 4방향장."""
from __future__ import annotations

from collections import defaultdict
from math import atan2, cos, sin, sqrt

from .core import MeshData, RemeshCancelled
from .surface import add, center, cross, dot, mul, sub, unit


def _check(cancelled):
    if cancelled and cancelled():
        raise RemeshCancelled("방향장 처리를 취소했습니다.")


def _normal(vertices, face):
    origin = vertices[face[0]]
    normal = (0., 0., 0.)
    for j in range(1, len(face)-1):
        normal = add(normal, cross(sub(vertices[face[j]], origin), sub(vertices[face[j+1]], origin)))
    return unit(normal)


def _frame(n):
    axis = min(((1.,0.,0.), (0.,1.,0.), (0.,0.,1.)), key=lambda a: abs(dot(a,n)))
    t = unit(sub(axis, mul(n, dot(axis,n))))
    return t, cross(n,t)


def _encode(direction, t, b):
    angle = atan2(dot(direction,b), dot(direction,t)) * 4
    return complex(cos(angle), sin(angle))


def _decode(value, t, b):
    angle = atan2(value.imag, value.real)/4
    return add(mul(t, cos(angle)), mul(b, sin(angle)))


def solve_field(mesh, guide_segments=(), *, iterations=16, cancelled=None):
    """면 접평면의 복소수 4승 표현으로 90도 동치를 보존한다."""
    vertices, faces = mesh.vertices, mesh.faces
    normals = [_normal(vertices,f) for f in faces]
    frames = [_frame(n) for n in normals]
    centers = [center([vertices[v] for v in f]) for f in faces]
    edge_faces = defaultdict(list)
    for i,f in enumerate(faces):
        for a,b in zip(f, (*f[1:],f[0])):
            edge_faces[tuple(sorted((a,b)))].append(i)
    neighbors = [[] for _ in faces]
    anchors = [0j for _ in faces]
    weights = [0.0 for _ in faces]
    curvature = [0j for _ in faces]
    for (a,b), indices in edge_faces.items():
        direction = unit(sub(vertices[b],vertices[a]))
        feature = (a,b) in mesh.hard_edges or len(indices)==1
        if feature:
            for i in indices:
                t,bt = frames[i]
                anchors[i] += 8*_encode(direction,t,bt)
                weights[i] += 8
        if len(indices)==2:
            i,j = indices
            if not feature:
                neighbors[i].append(j)
                neighbors[j].append(i)
            bend = max(0.,1-dot(normals[i],normals[j]))
            for k in indices:
                t,bt = frames[k]
                curvature[k] += bend*_encode(direction,t,bt)
    extent = max(max(v[a] for v in vertices)-min(v[a] for v in vertices) for a in range(3))
    radius2 = max(extent*extent*.04,1e-15)
    for i,p in enumerate(centers):
        if i%128==0:
            _check(cancelled)
        t,b = frames[i]
        if guide_segments:
            closest = None
            for start,end in guide_segments:
                d = sub(end,start)
                ratio = max(0.,min(1.,dot(sub(p,start),d)/max(dot(d,d),1e-30)))
                delta = sub(p,add(start,mul(d,ratio)))
                dist = dot(delta,delta)
                if closest is None or dist < closest[0]:
                    closest = (dist,d)
            dist,d = closest
            tangent = sub(d,mul(normals[i],dot(d,normals[i])))
            if dot(tangent,tangent)>1e-20:
                weight = 12*radius2/(radius2+dist)
                anchors[i] += weight*_encode(tangent,t,b)
                weights[i] += weight
        if abs(curvature[i])>1e-8:
            anchors[i] += .5*curvature[i]/abs(curvature[i])
            weights[i] += .5
    values = [a/abs(a) if abs(a)>1e-12 else 1+0j for a in anchors]
    for _ in range(iterations):
        _check(cancelled)
        directions = [_decode(z,*frames[i]) for i,z in enumerate(values)]
        updated = []
        for i in range(len(faces)):
            total = anchors[i] + .05*values[i]
            for j in neighbors[i]:
                d = directions[j]
                # 이웃 방향을 현재 접평면으로 옮겨 공통 4방향 표현으로 합산한다.
                tangent = sub(d,mul(normals[i],dot(d,normals[i])))
                if dot(tangent,tangent)>1e-15:
                    total += _encode(tangent,*frames[i])
            updated.append(total/abs(total) if abs(total)>1e-12 else values[i])
        values = updated
    return tuple(_decode(z,*frames[i]) for i,z in enumerate(values))


def alignment(vertices, face, direction):
    normal = _normal(vertices,face)
    t = unit(sub(direction,mul(normal,dot(direction,normal))))
    b = cross(normal,t)
    scores=[]
    for a,c in zip(face, (*face[1:],face[0])):
        d = unit(sub(vertices[c],vertices[a]))
        x,y = dot(d,t),dot(d,b)
        denom = x*x+y*y
        scores.append((x**4-6*x*x*y*y+y**4)/(denom*denom) if denom>1e-20 else -1.)
    return sum((s+1)*.5 for s in scores)/len(scores)


def optimize_quads(mesh, surface, guide_segments=(), *, iterations=5, cancelled=None):
    """방향장에 맞게 내부 정점을 이동하고 원본 표면에 재투영한다."""
    vertices = list(mesh.vertices)
    faces = mesh.faces
    directions = solve_field(mesh,guide_segments,cancelled=cancelled)
    edge_counts=defaultdict(int)
    vertex_faces=defaultdict(list)
    neighbors=defaultdict(set)
    for i,f in enumerate(faces):
        for v in f:
            vertex_faces[v].append(i)
        for a,b in zip(f,(*f[1:],f[0])):
            edge_counts[tuple(sorted((a,b)))]+=1
            neighbors[a].add(b)
            neighbors[b].add(a)
    fixed={v for edge,n in edge_counts.items() if n==1 or edge in mesh.hard_edges for v in edge}
    vertex_components = _map_vertices_to_surface_components(vertices, faces, vertex_faces, surface, fixed)
    # 특징선은 정점 위치를 유지한다. 내부 점만 표면 재투영과 함께 이동한다.
    for step in range(iterations):
        _check(cancelled)
        for v in sorted(neighbors):
            if v in fixed:
                continue
            if v%256==0:
                _check(cancelled)
            incident=vertex_faces[v]
            normal=unit(tuple(sum(_normal(vertices,faces[i])[a] for i in incident) for a in range(3)))
            t=unit(sub(directions[incident[0]],mul(normal,dot(directions[incident[0]],normal))))
            b=cross(normal,t)
            p=vertices[v]
            proposals=[]
            for n in sorted(neighbors[v]):
                delta=sub(p,vertices[n])
                axes=(t,b,mul(t,-1),mul(b,-1))
                axis=max(axes,key=lambda d:dot(delta,d))
                length=sqrt(dot(delta,delta))
                proposals.append(add(vertices[n],mul(axis,length)))
            ideal=center(proposals)
            candidate=add(p,mul(sub(ideal,p),.25))
            candidate,_,source_face=surface.nearest(candidate, component=vertex_components[v])
            if dot(surface.face_normals[source_face],normal)<0.:
                continue
            if _valid_move(vertices,faces,incident,v,candidate) and _surface_move_valid(vertices,faces,incident,v,candidate,surface,vertex_components[v]):
                vertices[v]=candidate
    # 분할로 생긴 내부 정점도 원본 표면에 맞춘다.
    for v,p in enumerate(vertices):
        if v%256==0:
            _check(cancelled)
        if v in fixed:
            continue
        normal=unit(tuple(sum(_normal(vertices,faces[i])[a] for i in vertex_faces[v]) for a in range(3)))
        candidate,_,source_face=surface.nearest(p, component=vertex_components[v])
        if dot(surface.face_normals[source_face],normal)<0.:
            continue
        if _valid_move(vertices,faces,vertex_faces[v],v,candidate) and _surface_move_valid(vertices,faces,vertex_faces[v],v,candidate,surface,vertex_components[v]):
            vertices[v]=candidate
    output=MeshData(tuple(vertices),faces,mesh.hard_edges)
    score=sum(alignment(vertices,f,directions[i]) for i,f in enumerate(faces))/len(faces)
    return output,score


def _map_vertices_to_surface_components(vertices, faces, vertex_faces, surface, fixed):
    if len(surface.component_faces) == 1:
        component = next(iter(surface.component_faces))
        return {vertex: component for vertex in vertex_faces}
    output_components = _output_face_components(faces)
    component_votes = defaultdict(lambda: defaultdict(int))
    # 이동하지 않는 경계·특징선만 있는 조각은 재투영할 표면을 찾을 필요가 없다.
    movable_components = {output_components[i] for i, face in enumerate(faces) if any(v not in fixed for v in face)}
    for face_index, face in enumerate(faces):
        output_component = output_components[face_index]
        if output_component not in movable_components:
            continue
        face_normal = _normal(vertices, face)
        for vertex in face:
            try:
                _, _, source_face = surface.nearest(vertices[vertex], normal=face_normal)
            except ValueError:
                _, _, source_face = surface.nearest(vertices[vertex])
            weight = 3 if vertex in fixed else 1
            component_votes[output_component][surface.face_components[source_face]] += weight

    output_to_source = {}
    for output_component, votes in component_votes.items():
        ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise ValueError("출력 패치와 원본 표면 컴포넌트 매핑이 모호합니다.")
        output_to_source[output_component] = ranked[0][0]

    vertex_components = {}
    for vertex, incident in vertex_faces.items():
        if vertex in fixed:
            continue
        votes = defaultdict(int)
        for face_index in incident:
            votes[output_to_source[output_components[face_index]]] += 1
        ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise ValueError("정점의 원본 표면 컴포넌트 매핑이 모호합니다.")
        vertex_components[vertex] = ranked[0][0]
    return vertex_components


def _output_face_components(faces):
    edge_faces = defaultdict(list)
    vertex_faces = defaultdict(list)
    for face_index, face in enumerate(faces):
        for vertex in face:
            vertex_faces[vertex].append(face_index)
        for a,b in zip(face,(*face[1:],face[0])):
            edge_faces[tuple(sorted((a,b)))].append(face_index)
    neighbors = defaultdict(set)
    for linked_faces in edge_faces.values():
        for face_index in linked_faces:
            neighbors[face_index].update(linked_faces)
    for linked_faces in vertex_faces.values():
        for face_index in linked_faces:
            neighbors[face_index].update(linked_faces)
    components = [-1 for _ in faces]
    component = 0
    for start in range(len(faces)):
        if components[start] != -1:
            continue
        stack = [start]
        components[start] = component
        while stack:
            face_index = stack.pop()
            for neighbor in neighbors[face_index]:
                if components[neighbor] == -1:
                    components[neighbor] = component
                    stack.append(neighbor)
        component += 1
    return tuple(components)


def _surface_move_valid(vertices, faces, incident, vertex, point, surface, component):
    # 정점을 표면에 붙여도 주변 면이 표면을 크게 벗어나는 이동은 허용하지 않는다.
    for i in incident:
        positions=[point if v==vertex else vertices[v] for v in faces[i]]
        samples=[center(positions)]
        samples.extend(mul(add(positions[j],positions[(j+1)%len(positions)]),.5) for j in range(len(positions)))
        if any(surface.nearest(p,component=component)[1] > .04 for p in samples):
            return False
    return True


def _valid_move(vertices, faces, incident, vertex, point):
    from .engine import FACE_NORMAL_EPSILON

    for i in incident:
        face=faces[i]
        old=[vertices[v] for v in face]
        new=[point if v==vertex else vertices[v] for v in face]
        n=_normal(vertices,face)
        # 최종 엔진과 동일한 주축 투영에서도 볼록성과 방향을 유지한다.
        new_normal=(0.,0.,0.)
        for j in range(1,len(face)-1):
            new_normal=add(new_normal,cross(sub(new[j],new[0]),sub(new[j+1],new[0])))
        # 이동 한 번으로 최종 면적 검사를 통과하지 못하는 면을 만들지 않는다.
        if sqrt(dot(new_normal, new_normal)) <= FACE_NORMAL_EPSILON:
            return False
        axis=max(range(3),key=lambda a:abs(new_normal[a]))
        plane=[tuple(c for a,c in enumerate(p) if a!=axis) for p in new]
        turns=[]
        for j,p in enumerate(plane):
            a,b=plane[j-1],plane[(j+1)%len(plane)]
            turns.append((p[0]-a[0])*(b[1]-p[1])-(p[1]-a[1])*(b[0]-p[0]))
        if not (all(t>1e-13 for t in turns) or all(t < -1e-13 for t in turns)):
            return False
        for j in range(len(face)):
            turn=cross(sub(new[j],new[j-1]),sub(new[(j+1)%len(face)],new[j]))
            if dot(turn,n)<=1e-13:
                return False
        old_area=sum(sqrt(dot(cross(sub(old[j],old[0]),sub(old[j+1],old[0])),cross(sub(old[j],old[0]),sub(old[j+1],old[0])))) for j in range(1,len(face)-1))
        new_area=sum(sqrt(dot(cross(sub(new[j],new[0]),sub(new[j+1],new[0])),cross(sub(new[j],new[0]),sub(new[j+1],new[0])))) for j in range(1,len(face)-1))
        if new_area<.15*old_area:
            return False
    return True
