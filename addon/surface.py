"""원본 삼각 표면의 최근접점 검색과 재투영."""
from __future__ import annotations

from collections import defaultdict, deque
import heapq
from math import sqrt


def sub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def add(a, b):
    return tuple(a[i] + b[i] for i in range(3))


def mul(a, s):
    return tuple(x * s for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def unit(a):
    n = sqrt(dot(a, a))
    return mul(a, 1 / n) if n > 1e-15 else (0., 0., 0.)


def center(points):
    return tuple(sum(p[i] for p in points) / len(points) for i in range(3))


def closest_triangle(p, a, b, c):
    ab, ac, ap = sub(b, a), sub(c, a), sub(p, a)
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = sub(p, b)
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1*d4 - d3*d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return add(a, mul(ab, d1/(d1-d3)))
    cp = sub(p, c)
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5*d2 - d1*d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return add(a, mul(ac, d2/(d2-d6)))
    va = d3*d6 - d5*d4
    if va <= 0 and d4-d3 >= 0 and d5-d6 >= 0:
        return add(b, mul(sub(c, b), (d4-d3)/((d4-d3)+(d5-d6))))
    denom = va+vb+vc
    if abs(denom) < 1e-30:
        return min((a, b, c), key=lambda v: dot(sub(p, v), sub(p, v)))
    return add(a, add(mul(ab, vb/denom), mul(ac, vc/denom)))


class SurfaceIndex:
    def __init__(self, mesh):
        if not mesh.faces:
            raise ValueError("표면 인덱스는 최소 1개의 삼각형이 필요합니다.")
        for face_index, face in enumerate(mesh.faces):
            if len(face) != 3:
                raise ValueError(f"표면 인덱스 입력은 삼각형만 지원합니다: {face_index}번 면")
        mesh.validate()
        self.vertices = mesh.vertices
        self.faces = mesh.faces
        self.nodes = []
        self.centers = [center([mesh.vertices[i] for i in f]) for f in mesh.faces]
        self.face_normals = tuple(unit(cross(sub(mesh.vertices[f[1]], mesh.vertices[f[0]]), sub(mesh.vertices[f[2]], mesh.vertices[f[0]]))) for f in mesh.faces)
        self.face_components = _face_components(mesh.faces)
        self.component_faces = defaultdict(list)
        for face_index, component in enumerate(self.face_components):
            self.component_faces[component].append(face_index)
        self._build(list(range(len(mesh.faces))))

    def _build(self, indices):
        points = [self.vertices[v] for i in indices for v in self.faces[i]]
        low = tuple(min(p[a] for p in points) for a in range(3))
        high = tuple(max(p[a] for p in points) for a in range(3))
        node = len(self.nodes)
        self.nodes.append(None)
        if len(indices) <= 8:
            self.nodes[node] = (low, high, tuple(indices), None)
        else:
            axis = max(range(3), key=lambda a: high[a]-low[a])
            indices.sort(key=lambda i: (self.centers[i][axis], i))
            half = len(indices)//2
            children = (self._build(indices[:half]), self._build(indices[half:]))
            self.nodes[node] = (low, high, (), children)
        return node

    def nearest(self, p, *, component=None, normal=None, min_normal_dot=0.0):
        best_distance = float('inf')
        best_point, best_face = None, -1
        queue = [(0., 0)]
        while queue:
            distance, index = heapq.heappop(queue)
            if distance > best_distance:
                break
            low, high, faces, children = self.nodes[index]
            if children:
                for child in children:
                    lo, hi = self.nodes[child][:2]
                    bound = sum(max(lo[a]-p[a], 0., p[a]-hi[a])**2 for a in range(3))
                    if bound <= best_distance:
                        heapq.heappush(queue, (bound, child))
            for i in faces:
                if component is not None and self.face_components[i] != component:
                    continue
                if normal is not None and dot(self.face_normals[i], normal) < min_normal_dot:
                    continue
                a, b, c = (self.vertices[v] for v in self.faces[i])
                point = closest_triangle(p, a, b, c)
                dist = dot(sub(p, point), sub(p, point))
                if dist < best_distance:
                    best_point, best_distance, best_face = point, dist, i
        if best_face == -1:
            raise ValueError("조건에 맞는 최근접 표면을 찾지 못했습니다.")
        return best_point, sqrt(best_distance), best_face


def _face_components(faces):
    edge_faces = defaultdict(list)
    vertex_faces = defaultdict(list)
    for face_index, face in enumerate(faces):
        for vertex in face:
            vertex_faces[vertex].append(face_index)
        for first, second in zip(face, (*face[1:], face[0])):
            edge_faces[tuple(sorted((first, second)))].append(face_index)

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
        queue = deque([start])
        components[start] = component
        while queue:
            face_index = queue.popleft()
            for neighbor in neighbors[face_index]:
                if components[neighbor] == -1:
                    components[neighbor] = component
                    queue.append(neighbor)
        component += 1
    return tuple(components)
