"""축 정렬된 단일 T형 표면에 독립적인 쿼드 분기 격자를 만든다."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isclose, sqrt

from ..core import CancelledCallback, EngineInput, MeshData, RemeshCancelled, Vector3
from .quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout


_EPS = 1.0e-7
_MAX_CAGE_AXIS_CELLS = 1024
_MAX_CAGE_CELLS = 1000000


@dataclass(frozen=True)
class _Shape:
    bx0: float
    bx1: float
    by0: float
    by1: float
    z0: float
    z1: float
    ay0: float
    ay1: float
    az0: float
    az1: float
    ax1: float
    lower_guide: float
    upper_guide: float
    arm_guide: float


def try_remesh_branch_t(engine_input: EngineInput, *, cancelled: CancelledCallback | None = None) -> MeshData | None:
    """세 폐루프로 지정한 직선 몸통·팔의 사각 T 표면만 처리한다.

    이 경로는 원본의 삼각 연결이나 숨겨진 쿼드 대각선에 의존하지 않는다.
    완전한 축 정렬 사각 단면과 평평한 끝면을 벗어나면 안전하게 거절한다.
    """
    _check_cancelled(cancelled)
    mesh, settings = engine_input.mesh, engine_input.settings
    if (settings.symmetry_axes or mesh.hard_edges or settings.target_quad_count < 100
            or settings.target_quad_count > 20000 or not isclose(settings.density_scale, 1.0, abs_tol=_EPS)
            or (engine_input.density_values and max(engine_input.density_values) - min(engine_input.density_values) > _EPS)
            or any(len(face) != 3 for face in mesh.faces)):
        return None
    if not _closed_sphere(mesh):
        return None
    guides = _three_loops(engine_input)
    if guides is None:
        return None
    shape = _infer_shape(mesh, guides)
    if shape is None:
        return None
    section = min(shape.bx1 - shape.bx0, shape.ay1 - shape.ay0, shape.az1 - shape.az0)
    estimated_area = (2 * ((shape.bx1-shape.bx0) + (shape.by1-shape.by0)) * (shape.z1-shape.z0)
                      + 2 * ((shape.ay1-shape.ay0) + (shape.az1-shape.az0)) * (shape.ax1-shape.bx1))
    center = max(2, round(section * sqrt(settings.target_quad_count / max(estimated_area, _EPS))))
    choices: list[tuple[int, MeshData, float]] = []
    for divisions in range(max(2, center - 3), min(48, center + 3) + 1):
        _check_cancelled(cancelled)
        candidate, spacing = _make_cage(shape, section / divisions, cancelled)
        if candidate is not None:
            choices.append((abs(len(candidate.faces) - settings.target_quad_count), candidate, spacing))
    if not choices:
        return None
    _, output, spacing = min(choices, key=lambda item: item[0])
    if abs(len(output.faces) - settings.target_quad_count) / settings.target_quad_count > 0.05:
        return None
    if not _closed_sphere(output):
        return None
    expectations = tuple(EdgePathExpectation(f"branch:{i}", points, closed=True, tolerance=max(spacing * 0.35, _EPS))
                         for i, points in enumerate(guides))
    report = validate_layout(output, LayoutExpectations(
        loops=expectations, boundary_edge_count=0, max_face_aspect_ratio=5.0,
    ))
    if not report.ok:
        return None
    # 양방향 검사는 원본의 다른 삼각 배치나 누락된 표면을 성공으로 오인하지 않게 한다.
    distance = measure_bidirectional_sample_distance(mesh, output, cancelled=cancelled)
    if distance.max_distance > max(section * 1.0e-4, _EPS):
        return None
    return output


def _three_loops(data: EngineInput) -> tuple[tuple[Vector3, ...], ...] | None:
    loops: list[tuple[Vector3, ...]] = []
    for guide in data.guide_curves:
        for points, kind, closed in zip(guide.splines, guide.kind, guide.closed):
            if kind != "LOOP" or not closed or len(points) < 4:
                return None
            loop = tuple(points[:-1] if _distance(points[0], points[-1]) < _EPS else points)
            if len(loop) < 4:
                return None
            loops.append(loop)
    if len(loops) != 3:
        return None
    names = {guide.name for guide in data.guide_curves}
    if data.settings.guide_curve_names and not set(data.settings.guide_curve_names).issubset(names):
        return None
    return tuple(loops)


def _infer_shape(mesh: MeshData, loops: tuple[tuple[Vector3, ...], ...]) -> _Shape | None:
    bounds = tuple((min(point[axis] for point in mesh.vertices), max(point[axis] for point in mesh.vertices))
                   for axis in range(3))
    section_extent = min(high - low for low, high in bounds)
    tol = max(section_extent * 1.0e-5, _EPS)
    body: list[tuple[float, tuple[Vector3, ...]]] = []
    arm: list[tuple[float, tuple[Vector3, ...]]] = []
    for loop in loops:
        if max(point[2] for point in loop) - min(point[2] for point in loop) <= tol:
            body.append((sum(point[2] for point in loop) / len(loop), loop))
        elif max(point[0] for point in loop) - min(point[0] for point in loop) <= tol:
            arm.append((sum(point[0] for point in loop) / len(loop), loop))
        else:
            return None
    if len(body) != 2 or len(arm) != 1:
        return None
    body.sort(key=lambda item: item[0])
    bx0 = min(point[0] for point in body[0][1])
    bx1 = max(point[0] for point in body[0][1])
    by0 = min(point[1] for point in body[0][1])
    by1 = max(point[1] for point in body[0][1])
    for _, loop in body:
        if not _rectangle_perimeter(loop, 0, 1, (bx0, bx1), (by0, by1), tol):
            return None
    ay0 = min(point[1] for point in arm[0][1])
    ay1 = max(point[1] for point in arm[0][1])
    az0 = min(point[2] for point in arm[0][1])
    az1 = max(point[2] for point in arm[0][1])
    if not _rectangle_perimeter(arm[0][1], 1, 2, (ay0, ay1), (az0, az1), tol):
        return None
    shape = _Shape(bx0, bx1, by0, by1, bounds[2][0], bounds[2][1], ay0, ay1, az0, az1,
                   bounds[0][1], body[0][0], body[1][0], arm[0][0])
    if not (bx0 < bx1 < shape.arm_guide < shape.ax1
            and shape.z0 < shape.lower_guide < az0 < az1 < shape.upper_guide < shape.z1
            and by0 < ay0 < ay1 < by1 and abs(bounds[0][0] - bx0) <= tol
            and abs(bounds[1][0] - by0) <= tol and abs(bounds[1][1] - by1) <= tol):
        return None
    return shape


def _rectangle_perimeter(points, axis_a, axis_b, span_a, span_b, tol):
    corners = set()
    if len({tuple(round(value / tol) for value in point) for point in points}) != len(points):
        return False
    for point in points:
        a, b = point[axis_a], point[axis_b]
        if not (span_a[0] - tol <= a <= span_a[1] + tol and span_b[0] - tol <= b <= span_b[1] + tol):
            return False
        if min(abs(a-span_a[0]), abs(a-span_a[1]), abs(b-span_b[0]), abs(b-span_b[1])) > tol:
            return False
        for ia, va in enumerate(span_a):
            for ib, vb in enumerate(span_b):
                if abs(a-va) <= tol and abs(b-vb) <= tol:
                    corners.add((ia, ib))
    if len(corners) != 4:
        return False
    for first, second in zip(points, (*points[1:], points[0])):
        # 폐곡선의 각 구간은 사각 단면의 한 변을 따라야 한다.
        a0, b0 = first[axis_a], first[axis_b]
        a1, b1 = second[axis_a], second[axis_b]
        same_edge = any(abs(a0-value) <= tol and abs(a1-value) <= tol for value in span_a)
        same_edge |= any(abs(b0-value) <= tol and abs(b1-value) <= tol for value in span_b)
        if not same_edge:
            return False
    return True


def _axis_values(breaks: tuple[float, ...], step: float) -> tuple[float, ...]:
    values = [breaks[0]]
    for start, end in zip(breaks, breaks[1:]):
        if end - start <= _EPS:
            continue
        count = max(1, round((end - start) / step))
        values.extend(start + (end - start) * index / count for index in range(1, count + 1))
    return tuple(values)


def _make_cage(shape: _Shape, spacing: float, cancelled: CancelledCallback | None) -> tuple[MeshData | None, float]:
    breaks = (
        (shape.bx0, shape.bx1, shape.arm_guide, shape.ax1),
        (shape.by0, shape.ay0, shape.ay1, shape.by1),
        (shape.z0, shape.lower_guide, shape.az0, shape.az1, shape.upper_guide, shape.z1),
    )
    counts = tuple(sum(max(1, round((end - start) / spacing)) for start, end in zip(axis, axis[1:])
                       if end - start > _EPS) for axis in breaks)
    if max(counts) > _MAX_CAGE_AXIS_CELLS or counts[0] * counts[1] * counts[2] > _MAX_CAGE_CELLS:
        return None, spacing
    xs, ys, zs = (_axis_values(axis, spacing) for axis in breaks)
    occupied = set()
    for i in range(len(xs)-1):
        _check_cancelled(cancelled)
        x = (xs[i]+xs[i+1])/2
        for j in range(len(ys)-1):
            y = (ys[j]+ys[j+1])/2
            for k in range(len(zs)-1):
                z = (zs[k]+zs[k+1])/2
                if x < shape.bx1 or (shape.ay0 < y < shape.ay1 and shape.az0 < z < shape.az1):
                    occupied.add((i,j,k))
    vertices: list[Vector3] = []
    indices: dict[tuple[int,int,int], int] = {}
    faces: list[tuple[int,int,int,int]] = []
    def vertex(key):
        if key not in indices:
            indices[key] = len(vertices)
            vertices.append((xs[key[0]], ys[key[1]], zs[key[2]]))
        return indices[key]
    for i,j,k in sorted(occupied):
        _check_cancelled(cancelled)
        surfaces = (
            ((i+1,j,k) not in occupied, ((i+1,j,k),(i+1,j+1,k),(i+1,j+1,k+1),(i+1,j,k+1))),
            ((i-1,j,k) not in occupied, ((i,j,k+1),(i,j+1,k+1),(i,j+1,k),(i,j,k))),
            ((i,j+1,k) not in occupied, ((i,j+1,k),(i,j+1,k+1),(i+1,j+1,k+1),(i+1,j+1,k))),
            ((i,j-1,k) not in occupied, ((i+1,j,k),(i+1,j,k+1),(i,j,k+1),(i,j,k))),
            ((i,j,k+1) not in occupied, ((i,j,k+1),(i+1,j,k+1),(i+1,j+1,k+1),(i,j+1,k+1))),
            ((i,j,k-1) not in occupied, ((i,j+1,k),(i+1,j+1,k),(i+1,j,k),(i,j,k))),
        )
        for exposed, corners in surfaces:
            if exposed:
                faces.append(tuple(vertex(key) for key in corners))
    if not faces:
        return None, spacing
    return MeshData(tuple(vertices), tuple(faces)), spacing


def _closed_sphere(mesh: MeshData) -> bool:
    edges = defaultdict(list)
    neighbors = defaultdict(set)
    for face_index, face in enumerate(mesh.faces):
        if len(set(face)) != len(face):
            return False
        for a,b in zip(face, (*face[1:], face[0])):
            edges[(min(a,b),max(a,b))].append(face_index)
            neighbors[a].add(b)
            neighbors[b].add(a)
    if any(len(owners) != 2 for owners in edges.values()):
        return False
    if len(mesh.vertices) - len(edges) + len(mesh.faces) != 2:
        return False
    reached = {0}
    queue = deque((0,))
    while queue:
        for neighbor in neighbors[queue.popleft()]:
            if neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    if len(reached) != len(mesh.vertices):
        return False
    return validate_layout(mesh).metrics.bowtie_vertex_count == 0


def _distance(a: Vector3, b: Vector3) -> float:
    return sqrt(sum((a[i]-b[i])**2 for i in range(3)))


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("리메시 작업이 취소되었습니다.")
