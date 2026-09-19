"""닫힌 볼록 표면의 독립 LOOP 가이드 주위에 새 쿼드 패치를 만든다."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil, floor, isclose, sqrt
from typing import Sequence

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3
from ..surface import SurfaceIndex, cross, dot, sub


_BASES = (
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    ((-1, 0, 0), (0, -1, 0), (0, 0, 1)),
    ((0, 1, 0), (-1, 0, 0), (0, 0, 1)),
    ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
    ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
)
_EPS = 1.0e-9
_MAX_TARGET = 20000


@dataclass(frozen=True)
class _Patch:
    face: int
    x0: int
    x1: int
    y0: int
    y1: int
    guide: tuple[Vector3, ...]
    uv: tuple[tuple[float, float], ...]


def try_remesh_face_patch(engine_input: EngineInput, *, cancelled: CancelledCallback | None = None) -> MeshData | None:
    """볼록 단일 컴포넌트에서 분리된 LOOP마다 실제 새 쿼드 띠를 만든다.

    가이드는 같은 큐브 투영면 안에 있어야 하며, 서로 겹치지 않는 평면
    단순 폐곡선이어야 한다. 지원 범위를 벗어나면 원본 메시를 바꾸지 않고
    None을 반환한다.
    """
    _check_cancelled(cancelled)
    if not _supported(engine_input):
        return None
    source = engine_input.mesh
    if not _closed_connected(source):
        return None
    origin = _centroid(source.vertices)
    triangles = _triangulate(source)
    if triangles is None:
        return None
    surface = SurfaceIndex(triangles)
    target = engine_input.settings.target_quad_count
    guides = _guides(engine_input)
    if not guides:
        return None
    choices = []
    center_grid = round(sqrt(target / 6))
    for grid_candidate in range(max(6, center_grid - 4), min(96, center_grid + 4) + 1):
        candidate = _make_patches(guides, origin, grid_candidate, surface)
        if candidate is None:
            continue
        predicted = 6 * grid_candidate * grid_candidate + 2 * sum(2 * (p.x1 - p.x0 + p.y1 - p.y0) for p in candidate)
        choices.append((abs(predicted - target), grid_candidate, candidate))
    if not choices:
        return None
    _, grid, patches = min(choices, key=lambda item: item[0])
    grid_adjustments = _shared_boundary_adjustments(patches, grid)

    vertices: list[Vector3] = []
    faces: list[tuple[int, int, int, int]] = []
    vertex_lookup: dict[tuple[int, int, int], int] = {}

    def add_direction(direction: Vector3, key: tuple[int, int, int] | None = None) -> int | None:
        if key is not None and key in vertex_lookup:
            return vertex_lookup[key]
        point = _ray_point(surface, origin, direction)
        if point is None:
            return None
        index = len(vertices)
        vertices.append(point)
        if key is not None:
            vertex_lookup[key] = index
        return index

    grids: list[list[list[int]]] = []
    for face_index, (normal, axis_u, axis_v) in enumerate(_BASES):
        _check_cancelled(cancelled)
        face_grid: list[list[int]] = []
        for j in range(grid + 1):
            _check_cancelled(cancelled)
            row: list[int] = []
            for i in range(grid + 1):
                coordinate_u = 2 * i / grid - 1
                coordinate_v = 2 * j / grid - 1
                adjusted_u, adjusted_v = grid_adjustments.get((face_index, i, j), (coordinate_u, coordinate_v))
                direction = tuple(normal[k] + adjusted_u * axis_u[k] + adjusted_v * axis_v[k] for k in range(3))
                key_direction = tuple(normal[k] + coordinate_u * axis_u[k] + coordinate_v * axis_v[k] for k in range(3))
                key = tuple(round(grid * coordinate) for coordinate in key_direction)
                index = add_direction(direction, key)
                if index is None:
                    return None
                row.append(index)
            face_grid.append(row)
        grids.append(face_grid)
        for j in range(grid):
            for i in range(grid):
                if any(p.face == face_index and p.x0 <= i < p.x1 and p.y0 <= j < p.y1 for p in patches):
                    continue
                faces.append((face_grid[j][i], face_grid[j][i + 1], face_grid[j + 1][i + 1], face_grid[j + 1][i]))

    for patch in patches:
        _check_cancelled(cancelled)
        nx, ny = patch.x1 - patch.x0, patch.y1 - patch.y0
        if nx < 2 or ny < 2:
            return None
        grid_face = grids[patch.face]
        boundary = _rectangle_boundary(grid_face, patch)
        guide_uv = _resample_guide_uv(patch.uv, nx, ny)
        if guide_uv is None or len(guide_uv) != len(boundary):
            return None
        loop: list[int] = []
        for u, v in guide_uv:
            index = add_direction(_direction(patch.face, u, v))
            if index is None:
                return None
            loop.append(index)
        middle: list[int] = []
        for k, (u, v) in enumerate(guide_uv):
            boundary_point = vertices[boundary[k]]
            boundary_uv = _project_uv(sub(boundary_point, origin), patch.face)
            if boundary_uv is None:
                return None
            separation = _distance(boundary_uv, (u, v))
            fraction = max(0.5, min(0.7, 0.5 + 2 * (separation - 0.05)))
            midpoint = _direction(
                patch.face,
                boundary_uv[0] * (1 - fraction) + u * fraction,
                boundary_uv[1] * (1 - fraction) + v * fraction,
            )
            index = add_direction(midpoint)
            if index is None:
                return None
            middle.append(index)
        for k in range(len(loop)):
            next_k = (k + 1) % len(loop)
            faces.append((boundary[k], boundary[next_k], middle[next_k], middle[k]))
            faces.append((middle[k], middle[next_k], loop[next_k], loop[k]))

        cap: list[list[int]] = [[-1] * (nx + 1) for _ in range(ny + 1)]
        for k in range(nx):
            cap[0][k] = loop[k]
            cap[ny][nx - k] = loop[nx + ny + k]
        for k in range(ny):
            cap[k][nx] = loop[nx + k]
            cap[ny - k][0] = loop[2 * nx + ny + k]
        for j in range(1, ny):
            _check_cancelled(cancelled)
            for i in range(1, nx):
                u, v = _coons(guide_uv, nx, ny, i / nx, j / ny)
                index = add_direction(_direction(patch.face, u, v))
                if index is None:
                    return None
                cap[j][i] = index
        if any(index < 0 for row in cap for index in row):
            return None
        for j in range(ny):
            for i in range(nx):
                faces.append((cap[j][i], cap[j][i + 1], cap[j + 1][i + 1], cap[j + 1][i]))

    if abs(len(faces) - target) / target > 0.18:
        return None
    if _signed_volume(source) < 0:
        faces = [tuple(reversed(face)) for face in faces]
    used = sorted({index for face in faces for index in face})
    remap = {old: new for new, old in enumerate(used)}
    result = MeshData(
        tuple(vertices[index] for index in used),
        tuple(tuple(remap[index] for index in face) for face in faces),
    )
    if not _closed_connected(result) or any(len(set(face)) != 4 for face in faces):
        return None
    return result


def _supported(data: EngineInput) -> bool:
    settings = data.settings
    if settings.target_quad_count < 180 or settings.target_quad_count > _MAX_TARGET:
        return False
    if settings.symmetry_axes or data.mesh.hard_edges:
        return False
    if not isclose(settings.density_scale, 1.0, abs_tol=_EPS):
        return False
    if data.density_values and max(data.density_values) - min(data.density_values) > _EPS:
        return False
    return True


def _guides(data: EngineInput) -> tuple[tuple[Vector3, ...], ...] | None:
    loops: list[tuple[Vector3, ...]] = []
    for guide in data.guide_curves:
        for spline, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            if kind != "LOOP" or not closed:
                return None
            points = tuple(spline[:-1] if len(spline) > 3 and _distance(spline[0], spline[-1]) < _EPS else spline)
            if len(points) < 4:
                return None
            loops.append(points)
    return tuple(loops)


def _make_patches(guides, origin, grid, surface) -> tuple[_Patch, ...] | None:
    patches: list[_Patch] = []
    for guide in guides:
        center = _centroid(guide)
        delta = sub(center, origin)
        face = max(range(6), key=lambda index: dot(delta, _BASES[index][0]))
        uv: list[tuple[float, float]] = []
        for point in guide:
            projected = _project_uv(sub(point, origin), face)
            if projected is None or abs(projected[0]) >= 0.88 or abs(projected[1]) >= 0.88:
                return None
            _, distance, _ = surface.nearest(point)
            if distance > max(_distance(center, origin) * 0.015, 1.0e-5):
                return None
            uv.append(projected)
        if _polygon_area(uv) < 0:
            uv.reverse()
        if _polygon_area(uv) < 1.0e-5 or not _simple_polygon(uv) or not _convex_polygon(uv):
            return None
        lo_u, hi_u = min(p[0] for p in uv), max(p[0] for p in uv)
        lo_v, hi_v = min(p[1] for p in uv), max(p[1] for p in uv)
        x0 = floor((lo_u + 1) * grid / 2)
        x1 = ceil((hi_u + 1) * grid / 2)
        y0 = floor((lo_v + 1) * grid / 2)
        y1 = ceil((hi_v + 1) * grid / 2)
        if min(x0, y0) < 1 or max(x1, y1) > grid - 1:
            return None
        patch = _Patch(face, x0, x1, y0, y1, guide, tuple(uv))
        patches.append(patch)
    if any(_patches_overlap(first, second) for index, first in enumerate(patches) for second in patches[index + 1:]):
        return None
    for index, patch in enumerate(patches):
        for side in ("x0", "x1", "y0", "y1"):
            if side == "x0":
                candidate = _Patch(patch.face, patch.x0 - 1, patch.x1, patch.y0, patch.y1, patch.guide, patch.uv)
            elif side == "x1":
                candidate = _Patch(patch.face, patch.x0, patch.x1 + 1, patch.y0, patch.y1, patch.guide, patch.uv)
            elif side == "y0":
                candidate = _Patch(patch.face, patch.x0, patch.x1, patch.y0 - 1, patch.y1, patch.guide, patch.uv)
            else:
                candidate = _Patch(patch.face, patch.x0, patch.x1, patch.y0, patch.y1 + 1, patch.guide, patch.uv)
            if min(candidate.x0, candidate.y0) < 1 or max(candidate.x1, candidate.y1) > grid - 1:
                continue
            if any(_patches_overlap(candidate, other) for other_index, other in enumerate(patches) if other_index != index):
                continue
            patches[index] = patch = candidate
    return tuple(patches)


def _patches_overlap(first: _Patch, second: _Patch) -> bool:
    return first.face == second.face and not (
        first.x1 <= second.x0 or second.x1 <= first.x0
        or first.y1 <= second.y0 or second.y1 <= first.y0
    )


def _shared_boundary_adjustments(patches: Sequence[_Patch], grid: int) -> dict[tuple[int, int, int], tuple[float, float]]:
    adjusted: dict[tuple[int, int, int], tuple[float, float]] = {}

    def set_coordinate(face: int, i: int, j: int, axis: int, value: float) -> None:
        key = (face, i, j)
        coordinate = list(adjusted.get(key, (2 * i / grid - 1, 2 * j / grid - 1)))
        coordinate[axis] = value
        adjusted[key] = (coordinate[0], coordinate[1])

    for first_index, first in enumerate(patches):
        for second in patches[first_index + 1:]:
            if first.face != second.face:
                continue
            if first.y1 == second.y0 or second.y1 == first.y0:
                lower, upper = (first, second) if first.y1 == second.y0 else (second, first)
                lo, hi = max(first.x0, second.x0), min(first.x1, second.x1)
                if lo <= hi:
                    value = (max(point[1] for point in lower.uv) + min(point[1] for point in upper.uv)) / 2
                    for i in range(lo, hi + 1):
                        set_coordinate(first.face, i, lower.y1, 1, value)
            if first.x1 == second.x0 or second.x1 == first.x0:
                left, right = (first, second) if first.x1 == second.x0 else (second, first)
                lo, hi = max(first.y0, second.y0), min(first.y1, second.y1)
                if lo <= hi:
                    value = (max(point[0] for point in left.uv) + min(point[0] for point in right.uv)) / 2
                    for j in range(lo, hi + 1):
                        set_coordinate(first.face, left.x1, j, 0, value)
    return adjusted


def _rectangle_boundary(grid: Sequence[Sequence[int]], patch: _Patch) -> tuple[int, ...]:
    return (
        tuple(grid[patch.y0][i] for i in range(patch.x0, patch.x1))
        + tuple(grid[j][patch.x1] for j in range(patch.y0, patch.y1))
        + tuple(grid[patch.y1][i] for i in range(patch.x1, patch.x0, -1))
        + tuple(grid[j][patch.x0] for j in range(patch.y1, patch.y0, -1))
    )


def _resample_guide_uv(points: Sequence[tuple[float, float]], nx: int, ny: int) -> tuple[tuple[float, float], ...] | None:
    corners = (
        min(range(len(points)), key=lambda i: points[i][0] + points[i][1]),
        max(range(len(points)), key=lambda i: points[i][0] - points[i][1]),
        max(range(len(points)), key=lambda i: points[i][0] + points[i][1]),
        min(range(len(points)), key=lambda i: points[i][0] - points[i][1]),
    )
    if len(set(corners)) != 4 or not (0 < (corners[1] - corners[0]) % len(points) < (corners[2] - corners[0]) % len(points) < (corners[3] - corners[0]) % len(points)):
        return None
    output: list[tuple[float, float]] = []
    for side, count in enumerate((nx, ny, nx, ny)):
        start, end = corners[side], corners[(side + 1) % 4]
        arc = [points[start]]
        cursor = start
        while cursor != end:
            cursor = (cursor + 1) % len(points)
            arc.append(points[cursor])
        while len(arc) - 1 < count:
            longest = max(range(len(arc) - 1), key=lambda k: _distance(arc[k], arc[k + 1]))
            arc.insert(longest + 1, _lerp(arc[longest], arc[longest + 1], 0.5))
        if len(arc) - 1 == count:
            output.extend(arc[:-1])
            continue
        lengths = [0.0]
        for a, b in zip(arc, arc[1:]):
            lengths.append(lengths[-1] + _distance(a, b))
        if lengths[-1] < _EPS:
            return None
        for index in range(count):
            target = lengths[-1] * index / count
            segment = next(k for k in range(len(arc) - 1) if lengths[k + 1] >= target - _EPS)
            fraction = (target - lengths[segment]) / max(lengths[segment + 1] - lengths[segment], _EPS)
            output.append(_lerp(arc[segment], arc[segment + 1], fraction))
    return tuple(output)


def _coons(loop, nx, ny, u, v):
    def sample(side, amount):
        scaled = amount * (nx if side in (0, 2) else ny)
        position = min(int(floor(scaled)), (nx if side in (0, 2) else ny) - 1)
        fraction = scaled - position
        if side == 0:
            a, b = loop[position], loop[(position + 1) % len(loop)]
        elif side == 1:
            a, b = loop[nx + position], loop[(nx + position + 1) % len(loop)]
        elif side == 2:
            a, b = loop[nx + ny + position], loop[(nx + ny + position + 1) % len(loop)]
        else:
            a, b = loop[2 * nx + ny + position], loop[(2 * nx + ny + position + 1) % len(loop)]
        return _lerp(a, b, fraction)

    bottom, right, top, left = sample(0, u), sample(1, v), sample(2, 1 - u), sample(3, 1 - v)
    p00, p10, p11, p01 = loop[0], loop[nx], loop[nx + ny], loop[2 * nx + ny]
    bilinear = tuple((1-u)*(1-v)*p00[k]+u*(1-v)*p10[k]+u*v*p11[k]+(1-u)*v*p01[k] for k in range(2))
    return tuple((1-v)*bottom[k]+v*top[k]+(1-u)*left[k]+u*right[k]-bilinear[k] for k in range(2))


def _project_uv(direction: Vector3, face: int) -> tuple[float, float] | None:
    normal, axis_u, axis_v = _BASES[face]
    denominator = dot(direction, normal)
    if denominator <= _EPS:
        return None
    return dot(direction, axis_u) / denominator, dot(direction, axis_v) / denominator


def _direction(face: int, u: float, v: float) -> Vector3:
    normal, axis_u, axis_v = _BASES[face]
    return tuple(normal[k] + u * axis_u[k] + v * axis_v[k] for k in range(3))


def _ray_point(surface: SurfaceIndex, origin: Vector3, direction: Vector3) -> Vector3 | None:
    length = sqrt(dot(direction, direction))
    if length <= _EPS:
        return None
    ray = tuple(value / length for value in direction)
    best = float("inf")
    stack = [0]
    while stack:
        node = stack.pop()
        low, high, indices, children = surface.nodes[node]
        if not _ray_box(origin, ray, low, high, best):
            continue
        if children:
            stack.extend(children)
            continue
        for face_index in indices:
            a, b, c = (surface.vertices[index] for index in surface.faces[face_index])
            distance = _ray_triangle(origin, ray, a, b, c)
            if distance is not None and distance < best:
                best = distance
    return tuple(origin[k] + best * ray[k] for k in range(3)) if best < float("inf") else None


def _ray_box(origin, ray, low, high, maximum):
    minimum = 0.0
    for axis in range(3):
        if abs(ray[axis]) < _EPS:
            if origin[axis] < low[axis] or origin[axis] > high[axis]:
                return False
            continue
        first, second = (low[axis] - origin[axis]) / ray[axis], (high[axis] - origin[axis]) / ray[axis]
        minimum, maximum = max(minimum, min(first, second)), min(maximum, max(first, second))
        if minimum > maximum:
            return False
    return True


def _ray_triangle(origin, ray, a, b, c):
    edge_a, edge_b = sub(b, a), sub(c, a)
    vector = cross(ray, edge_b)
    denominator = dot(edge_a, vector)
    if abs(denominator) < 1.0e-12:
        return None
    inverse = 1.0 / denominator
    offset = sub(origin, a)
    u = dot(offset, vector) * inverse
    if u < -_EPS or u > 1 + _EPS:
        return None
    vector_q = cross(offset, edge_a)
    v = dot(ray, vector_q) * inverse
    if v < -_EPS or u + v > 1 + _EPS:
        return None
    distance = dot(edge_b, vector_q) * inverse
    return distance if distance > _EPS else None


def _triangulate(mesh: MeshData) -> MeshData | None:
    faces: list[tuple[int, int, int]] = []
    for face in mesh.faces:
        if len(face) > 4:
            return None
        for k in range(1, len(face) - 1):
            faces.append((face[0], face[k], face[k + 1]))
    return MeshData(mesh.vertices, tuple(faces))


def _closed_connected(mesh: MeshData) -> bool:
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(mesh.faces):
        for a, b in zip(face, face[1:] + face[:1]):
            edge_faces[tuple(sorted((a, b)))].append(face_index)
    if not edge_faces or any(len(linked) != 2 for linked in edge_faces.values()):
        return False
    neighbors: list[set[int]] = [set() for _ in mesh.faces]
    for a, b in edge_faces.values():
        neighbors[a].add(b)
        neighbors[b].add(a)
    visited = {0}
    pending = [0]
    while pending:
        for next_face in neighbors[pending.pop()]:
            if next_face not in visited:
                visited.add(next_face)
                pending.append(next_face)
    return len(visited) == len(mesh.faces)


def _signed_volume(mesh: MeshData) -> float:
    total = 0.0
    for face in mesh.faces:
        a = mesh.vertices[face[0]]
        for index in range(1, len(face) - 1):
            b, c = mesh.vertices[face[index]], mesh.vertices[face[index + 1]]
            total += dot(a, cross(b, c)) / 6
    return total


def _centroid(points: Sequence[Vector3]) -> Vector3:
    return tuple(sum(point[k] for point in points) / len(points) for k in range(3))


def _distance(a, b) -> float:
    return sqrt(sum((a[k] - b[k]) ** 2 for k in range(len(a))))


def _lerp(a, b, t):
    return tuple(a[k] * (1 - t) + b[k] * t for k in range(len(a)))


def _polygon_area(points):
    return sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:] + points[:1])) / 2


def _simple_polygon(points):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    count = len(points)
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = points[j], points[(j + 1) % count]
            if orient(a, b, c) * orient(a, b, d) < 0 and orient(c, d, a) * orient(c, d, b) < 0:
                return False
    return True


def _convex_polygon(points):
    for index in range(len(points)):
        a, b, c = points[index], points[(index + 1) % len(points)], points[(index + 2) % len(points)]
        cross_z = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if cross_z < -_EPS:
            return False
    return True


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("리메시 작업이 취소되었습니다.")
