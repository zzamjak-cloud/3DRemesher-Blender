from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import cos, sqrt
from typing import Sequence

from .core import (
    CancelledCallback,
    EngineInput,
    GuideCurveData,
    MeshData,
    ProgressCallback,
    RemeshCancelled,
    RemeshQuality,
    RemeshResult,
    Vector3,
    analyze_mesh,
)


EPSILON = 1.0e-9
PLANAR_EPSILON = 1.0e-6
MAX_SOURCE_VERTICES = 20000
MAX_SOURCE_FACES = 10000
MAX_SOURCE_CORNERS = 50000
MAX_FACE_VERTICES = 256
MAX_GUIDE_SEGMENTS = 20000
MAX_OUTPUT_QUADS = 200000
MAX_SUBDIVISION_SEGMENTS = 128


@dataclass(frozen=True)
class _Patch:
    vertices: tuple[int, ...]
    source_edges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _Topology:
    edge_faces: dict[tuple[int, int], tuple[int, ...]]
    directed_edges: dict[tuple[int, int], tuple[tuple[int, int, int], ...]]
    face_normals: tuple[Vector3, ...]
    feature_edges: frozenset[tuple[int, int]]
    output_hard_edges: frozenset[tuple[int, int]]


def remesh(
    engine_input: EngineInput,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelledCallback | None = None,
) -> RemeshResult:
    mesh = engine_input.mesh
    settings = engine_input.settings
    warnings: list[str] = []
    unsupported_controls = _unsupported_controls(engine_input)

    _check_cancelled(cancelled)
    _report(progress, 0.02, "입력 검증")
    settings.validate()
    mesh.validate()
    _validate_size(mesh)
    topology = _validate_topology(mesh, settings.hard_edge_angle_degrees)

    if unsupported_controls:
        warnings.append(
            "대칭 또는 밀도 제어는 현재 엔진에서 적용되지 않았습니다: "
            + ", ".join(unsupported_controls)
        )

    _check_cancelled(cancelled)
    _report(progress, 0.18, "면 패치 생성")
    guide_segments = _guide_segments(engine_input.guide_curves)
    patches = _build_patches(mesh, topology, guide_segments)

    _check_cancelled(cancelled)
    _report(progress, 0.44, "기본 쿼드 생성")
    base_vertices, base_quads, base_hard_edges = _patches_to_quads(mesh, patches, topology.output_hard_edges)
    if not base_quads:
        raise ValueError("쿼드로 변환할 수 있는 패치가 없습니다.")
    if len(base_quads) > MAX_OUTPUT_QUADS:
        raise ValueError(f"기본 쿼드 수가 출력 상한을 초과합니다: {len(base_quads)} > {MAX_OUTPUT_QUADS}")

    segments, predicted_count = _choose_subdivision_segments(len(base_quads), settings.target_quad_count)
    _check_cancelled(cancelled)
    _report(progress, 0.68, "균일 세분화")
    output_vertices, output_faces, output_hard_edges = _subdivide_quads(
        base_vertices,
        base_quads,
        base_hard_edges,
        segments,
        cancelled,
    )

    output_mesh = MeshData(
        vertices=tuple(output_vertices),
        faces=tuple(output_faces),
        hard_edges=frozenset(output_hard_edges),
    )
    output_analysis = analyze_mesh(output_mesh)
    if output_analysis.non_manifold_edge_count > 0:
        raise ValueError("출력 메시가 비다양체 엣지를 포함해 리메시를 중단했습니다.")
    if output_analysis.degenerate_face_count > 0:
        raise ValueError("출력 메시가 퇴화 면을 포함해 리메시를 중단했습니다.")
    max_aspect, mean_aspect = _quad_aspect_stats(output_mesh.vertices, output_mesh.faces)

    if output_analysis.quad_count != settings.target_quad_count:
        warnings.append(
            f"목표 쿼드 수와 실제 쿼드 수가 다릅니다: "
            f"target={settings.target_quad_count}, actual={output_analysis.quad_count}"
        )
    if output_analysis.quad_count >= engine_input.analysis.face_count:
        warnings.append("현재 엔진은 원본 대비 면 감소나 단순화를 수행하지 않습니다.")
    if settings.target_quad_count < len(base_quads):
        warnings.append("목표가 기본 쿼드 수보다 작아 감소 없이 가장 낮은 세분화 결과를 반환했습니다.")

    _check_cancelled(cancelled)
    _report(progress, 1.0, "완료")
    return RemeshResult(
        mesh=output_mesh,
        quality=RemeshQuality(
            target_quad_count=settings.target_quad_count,
            actual_quad_count=output_analysis.quad_count,
            quad_ratio=output_analysis.quad_ratio,
            boundary_edge_count=output_analysis.boundary_edge_count,
            non_manifold_edge_count=output_analysis.non_manifold_edge_count,
            degenerate_face_count=output_analysis.degenerate_face_count,
            max_aspect_ratio=max_aspect,
            mean_aspect_ratio=mean_aspect,
        ),
        warnings=tuple(warnings),
        unsupported_controls=unsupported_controls,
    )


def _unsupported_controls(engine_input: EngineInput) -> tuple[str, ...]:
    controls: list[str] = []
    if engine_input.settings.symmetry_axes:
        controls.append("symmetry_axes")
    if engine_input.density_values or engine_input.settings.density_scale != 1.0:
        controls.append("density")
    return tuple(controls)


def _validate_size(mesh: MeshData) -> None:
    if len(mesh.vertices) > MAX_SOURCE_VERTICES:
        raise ValueError(f"소스 정점 수가 너무 많습니다: {len(mesh.vertices)} > {MAX_SOURCE_VERTICES}")
    if len(mesh.faces) > MAX_SOURCE_FACES:
        raise ValueError(f"소스 면 수가 너무 많습니다: {len(mesh.faces)} > {MAX_SOURCE_FACES}")
    corner_count = sum(len(face) for face in mesh.faces)
    if corner_count > MAX_SOURCE_CORNERS:
        raise ValueError(f"소스 면 코너 수가 너무 많습니다: {corner_count} > {MAX_SOURCE_CORNERS}")
    for face_index, face in enumerate(mesh.faces):
        if len(face) > MAX_FACE_VERTICES:
            raise ValueError(f"{face_index}번 면 정점 수가 너무 많습니다: {len(face)} > {MAX_FACE_VERTICES}")


def _validate_topology(mesh: MeshData, hard_edge_angle_degrees: float) -> _Topology:
    duplicate_keys: set[tuple[int, ...]] = set()
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    directed_edges: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    face_normals: list[Vector3] = []

    for face_index, face in enumerate(mesh.faces):
        if len(set(face)) != len(face):
            raise ValueError(f"{face_index}번 면에 중복 정점이 있어 퇴화되었습니다.")
        face_key = tuple(sorted(face))
        if face_key in duplicate_keys:
            raise ValueError(f"{face_index}번 면은 중복 면입니다.")
        duplicate_keys.add(face_key)
        if len(face) > 3 and _has_self_intersections(_project_face_for_validation(mesh.vertices, face)):
            raise ValueError(f"{face_index}번 면은 자기교차 n-gon이라 안전하게 처리할 수 없습니다.")

        normal = _polygon_normal(mesh.vertices, face)
        if _length(normal) <= EPSILON:
            raise ValueError(f"{face_index}번 면은 면적이 없어 퇴화되었습니다.")
        face_normals.append(_normalize(normal))

        for first, second in _face_edges(face):
            edge = _edge_key(first, second)
            edge_faces[edge].append(face_index)
            directed_edges[edge].append((face_index, first, second))

    for edge, faces in edge_faces.items():
        if len(faces) > 2:
            raise ValueError(f"{edge} 엣지가 3개 이상의 면에 연결된 비다양체입니다.")
        if len(faces) == 2:
            first = directed_edges[edge][0]
            second = directed_edges[edge][1]
            if (first[1], first[2]) == (second[1], second[2]):
                raise ValueError(f"{edge} 엣지를 공유하는 면들의 winding이 일관되지 않습니다.")

    _reject_bowtie_vertices(mesh.faces)

    hard_edges = {_edge_key(*edge) for edge in mesh.hard_edges}
    angle_limit = cos(hard_edge_angle_degrees * 3.141592653589793 / 180.0)
    feature_edges = set(hard_edges)
    output_hard_edges = set(hard_edges)
    for edge, faces in edge_faces.items():
        if len(faces) == 1:
            feature_edges.add(edge)
        elif len(faces) == 2:
            dot = _dot(face_normals[faces[0]], face_normals[faces[1]])
            if dot < angle_limit:
                feature_edges.add(edge)
                output_hard_edges.add(edge)

    return _Topology(
        edge_faces={edge: tuple(faces) for edge, faces in edge_faces.items()},
        directed_edges={edge: tuple(items) for edge, items in directed_edges.items()},
        face_normals=tuple(face_normals),
        feature_edges=frozenset(feature_edges),
        output_hard_edges=frozenset(output_hard_edges),
    )


def _reject_bowtie_vertices(faces: Sequence[Sequence[int]]) -> None:
    incident_faces: dict[int, set[int]] = defaultdict(set)
    connected_faces: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))

    for face_index, face in enumerate(faces):
        for vertex in face:
            incident_faces[vertex].add(face_index)
        for first, second in _face_edges(face):
            connected_faces[first][face_index].add(face_index)
            connected_faces[second][face_index].add(face_index)

    edge_to_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for first, second in _face_edges(face):
            edge_to_faces[_edge_key(first, second)].append(face_index)
    for (first, second), edge_faces in edge_to_faces.items():
        for face_index in edge_faces:
            connected_faces[first][face_index].update(edge_faces)
            connected_faces[second][face_index].update(edge_faces)

    for vertex, face_set in incident_faces.items():
        if len(face_set) <= 1:
            continue
        start = next(iter(face_set))
        seen = {start}
        queue: deque[int] = deque([start])
        while queue:
            face_index = queue.popleft()
            for neighbor in connected_faces[vertex][face_index]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        if seen != face_set:
            raise ValueError(f"{vertex}번 정점은 bowtie 위상입니다.")


def _build_patches(
    mesh: MeshData,
    topology: _Topology,
    guide_segments: Sequence[tuple[Vector3, Vector3]],
) -> list[_Patch]:
    triangle_patches: list[tuple[int, tuple[int, int, int]]] = []
    patches: list[_Patch] = []

    for face_index, face in enumerate(mesh.faces):
        if len(face) == 3:
            triangle_patches.append((face_index, tuple(face)))
        elif len(face) == 4 and _is_planar_convex(mesh.vertices, face):
            patches.append(_Patch(vertices=tuple(face), source_edges=tuple(_face_edges(face))))
        else:
            triangles = _ear_clip_face(mesh.vertices, face, face_index)
            for triangle in triangles:
                triangle_patches.append((face_index, triangle))

    paired_triangles = _pair_triangles(mesh, triangle_patches, topology.feature_edges, guide_segments)
    used_triangles = set(paired_triangles)
    used_triangles.update(value for pair in paired_triangles.values() for value in pair)

    for left_index, (left, right) in paired_triangles.items():
        if left_index != left:
            continue
        left_face = triangle_patches[left][1]
        right_face = triangle_patches[right][1]
        quad = _quad_from_triangles(left_face, right_face)
        patches.append(_Patch(vertices=quad, source_edges=_patch_source_edges(quad)))

    for index, (_, triangle) in enumerate(triangle_patches):
        if index not in used_triangles:
            patches.append(_Patch(vertices=triangle, source_edges=tuple(_face_edges(triangle))))

    return patches


def _pair_triangles(
    mesh: MeshData,
    triangle_patches: Sequence[tuple[int, tuple[int, int, int]]],
    feature_edges: frozenset[tuple[int, int]],
    guide_segments: Sequence[tuple[Vector3, Vector3]],
) -> dict[int, tuple[int, int]]:
    edge_to_triangles: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (_, triangle) in enumerate(triangle_patches):
        for edge in _face_edges(triangle):
            edge_to_triangles[_edge_key(*edge)].append(index)

    candidates: list[tuple[float, tuple[int, int], int, int]] = []
    for edge, indices in edge_to_triangles.items():
        if edge in feature_edges or len(indices) != 2:
            continue
        left, right = sorted(indices)
        quad = _quad_from_triangles(triangle_patches[left][1], triangle_patches[right][1])
        if not _is_planar_convex(mesh.vertices, quad):
            continue
        score = _quad_pair_score(mesh.vertices, quad, guide_segments)
        candidates.append((score, edge, left, right))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    paired: dict[int, tuple[int, int]] = {}
    used: set[int] = set()
    for _, _, left, right in candidates:
        if left in used or right in used:
            continue
        used.add(left)
        used.add(right)
        pair = (left, right)
        paired[left] = pair
        paired[right] = pair
    return paired


def _quad_from_triangles(left: Sequence[int], right: Sequence[int]) -> tuple[int, int, int, int]:
    shared = [vertex for vertex in left if vertex in right]
    if len(shared) != 2:
        raise ValueError("삼각형 쌍이 엣지를 공유하지 않습니다.")
    left_only = next(vertex for vertex in left if vertex not in shared)
    right_only = next(vertex for vertex in right if vertex not in shared)
    for first, second in _face_edges(left):
        if {first, second} == set(shared):
            return (second, left_only, first, right_only)
    return (shared[0], left_only, shared[1], right_only)


def _patch_source_edges(vertices: Sequence[int]) -> tuple[tuple[int, int], ...]:
    return tuple(_face_edges(vertices))


def _patches_to_quads(
    mesh: MeshData,
    patches: Sequence[_Patch],
    feature_edges: frozenset[tuple[int, int]],
) -> tuple[list[Vector3], list[tuple[int, int, int, int]], set[tuple[int, int]]]:
    output_vertices = [tuple(vertex) for vertex in mesh.vertices]
    edge_midpoints: dict[tuple[int, int], int] = {}
    quads: list[tuple[int, int, int, int]] = []
    hard_edges: set[tuple[int, int]] = set()

    def midpoint(first: int, second: int) -> int:
        edge = _edge_key(first, second)
        if edge not in edge_midpoints:
            output_vertices.append(_midpoint(output_vertices[first], output_vertices[second]))
            edge_midpoints[edge] = len(output_vertices) - 1
        return edge_midpoints[edge]

    for patch in patches:
        center = _centroid([output_vertices[index] for index in patch.vertices])
        output_vertices.append(center)
        center_index = len(output_vertices) - 1
        mids: list[int] = []
        vertices = patch.vertices
        for first, second in _face_edges(vertices):
            mids.append(midpoint(first, second))

        for index, vertex in enumerate(vertices):
            previous_mid = mids[index - 1]
            next_mid = mids[index]
            quads.append((vertex, next_mid, center_index, previous_mid))

        for index, (first, second) in enumerate(_face_edges(vertices)):
            if _edge_key(first, second) in feature_edges:
                mid = mids[index]
                hard_edges.add(_edge_key(first, mid))
                hard_edges.add(_edge_key(mid, second))

    return output_vertices, quads, hard_edges


def _choose_subdivision_segments(base_quad_count: int, target_quad_count: int) -> tuple[int, int]:
    max_segments_by_faces = int(sqrt(MAX_OUTPUT_QUADS / base_quad_count))
    max_segments = max(1, min(MAX_SUBDIVISION_SEGMENTS, max_segments_by_faces))
    desired = sqrt(target_quad_count / base_quad_count)
    candidates = {
        1,
        max_segments,
        max(1, min(max_segments, int(desired))),
        max(1, min(max_segments, int(desired) + 1)),
    }
    best_segments = min(
        candidates,
        key=lambda segments: (abs(base_quad_count * segments * segments - target_quad_count), segments),
    )
    return best_segments, base_quad_count * best_segments * best_segments


def _subdivide_quads(
    vertices: Sequence[Vector3],
    quads: Sequence[tuple[int, int, int, int]],
    hard_edges: set[tuple[int, int]],
    segments: int,
    cancelled: CancelledCallback | None,
) -> tuple[list[Vector3], list[tuple[int, int, int, int]], set[tuple[int, int]]]:
    if segments == 1:
        return list(vertices), list(quads), set(hard_edges)

    output_vertices = list(vertices)
    edge_points: dict[tuple[int, int, int, int], int] = {}
    output_faces: list[tuple[int, int, int, int]] = []

    def edge_point(start: int, end: int, step: int) -> int:
        if step == 0:
            return start
        if step == segments:
            return end
        low, high = _edge_key(start, end)
        key_step = step if (start, end) == (low, high) else segments - step
        key = (low, high, key_step, segments)
        if key not in edge_points:
            ratio = key_step / segments
            output_vertices.append(_lerp(output_vertices[low], output_vertices[high], ratio))
            edge_points[key] = len(output_vertices) - 1
        return edge_points[key]

    for quad_index, quad in enumerate(quads):
        if quad_index % 256 == 0:
            _check_cancelled(cancelled)
        v0, v1, v2, v3 = quad
        grid: list[list[int]] = []
        for y in range(segments + 1):
            row: list[int] = []
            for x in range(segments + 1):
                if y == 0:
                    row.append(edge_point(v0, v1, x))
                elif y == segments:
                    row.append(edge_point(v3, v2, x))
                elif x == 0:
                    row.append(edge_point(v0, v3, y))
                elif x == segments:
                    row.append(edge_point(v1, v2, y))
                else:
                    u = x / segments
                    v = y / segments
                    output_vertices.append(_bilinear(output_vertices[v0], output_vertices[v1], output_vertices[v2], output_vertices[v3], u, v))
                    row.append(len(output_vertices) - 1)
            grid.append(row)

        for y in range(segments):
            for x in range(segments):
                output_faces.append((grid[y][x], grid[y][x + 1], grid[y + 1][x + 1], grid[y + 1][x]))

    output_hard_edges: set[tuple[int, int]] = set()
    for first, second in hard_edges:
        chain = [edge_point(first, second, step) for step in range(segments + 1)]
        for current, following in zip(chain, chain[1:]):
            output_hard_edges.add(_edge_key(current, following))

    return output_vertices, output_faces, output_hard_edges


def _ear_clip_face(vertices: Sequence[Vector3], face: Sequence[int], face_index: int) -> list[tuple[int, int, int]]:
    points = _project_face(vertices, face)
    if _has_self_intersections(points):
        raise ValueError(f"{face_index}번 면은 자기교차 n-gon이라 안전하게 삼각화할 수 없습니다.")

    order = list(range(len(face)))
    reversed_for_clipping = _signed_area(points) < 0.0
    if reversed_for_clipping:
        order.reverse()
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(order) > 3:
        guard += 1
        if guard > len(face) * len(face):
            raise ValueError(f"{face_index}번 면은 안전하게 ear clipping할 수 없습니다.")
        clipped = False
        for offset, current in enumerate(order):
            previous = order[offset - 1]
            following = order[(offset + 1) % len(order)]
            if not _is_convex_corner(points[previous], points[current], points[following]):
                continue
            if any(
                _point_in_triangle(points[candidate], points[previous], points[current], points[following])
                for candidate in order
                if candidate not in {previous, current, following}
            ):
                continue
            triangles.append(
                _oriented_triangle(face[previous], face[current], face[following], reversed_for_clipping)
            )
            del order[offset]
            clipped = True
            break
        if not clipped:
            raise ValueError(f"{face_index}번 면은 안전하게 ear clipping할 수 없습니다.")
    triangles.append(_oriented_triangle(face[order[0]], face[order[1]], face[order[2]], reversed_for_clipping))
    return triangles


def _project_face(vertices: Sequence[Vector3], face: Sequence[int]) -> list[tuple[float, float]]:
    normal = _polygon_normal(vertices, face)
    return _project_face_with_normal(vertices, face, normal)


def _project_face_for_validation(vertices: Sequence[Vector3], face: Sequence[int]) -> list[tuple[float, float]]:
    normal = _polygon_normal(vertices, face)
    if _length(normal) <= EPSILON:
        normal = _fallback_face_normal(vertices, face)
    return _project_face_with_normal(vertices, face, normal)


def _project_face_with_normal(
    vertices: Sequence[Vector3],
    face: Sequence[int],
    normal: Vector3,
) -> list[tuple[float, float]]:
    axis = max(range(3), key=lambda index: abs(normal[index]))
    points: list[tuple[float, float]] = []
    for vertex_index in face:
        vertex = vertices[vertex_index]
        if axis == 0:
            points.append((vertex[1], vertex[2]))
        elif axis == 1:
            points.append((vertex[0], vertex[2]))
        else:
            points.append((vertex[0], vertex[1]))
    return points


def _fallback_face_normal(vertices: Sequence[Vector3], face: Sequence[int]) -> Vector3:
    origin = vertices[face[0]]
    for first_offset in range(1, len(face) - 1):
        first = _sub(vertices[face[first_offset]], origin)
        for second_offset in range(first_offset + 1, len(face)):
            second = _sub(vertices[face[second_offset]], origin)
            normal = (
                first[1] * second[2] - first[2] * second[1],
                first[2] * second[0] - first[0] * second[2],
                first[0] * second[1] - first[1] * second[0],
            )
            if _length(normal) > EPSILON:
                return normal
    return (0.0, 0.0, 1.0)


def _is_planar_convex(vertices: Sequence[Vector3], face: Sequence[int]) -> bool:
    if len(face) < 3:
        return False
    normal = _polygon_normal(vertices, face)
    normal_length = _length(normal)
    if normal_length <= EPSILON:
        return False
    unit_normal = _scale(normal, 1.0 / normal_length)
    origin = vertices[face[0]]
    for vertex_index in face[1:]:
        if abs(_dot(_sub(vertices[vertex_index], origin), unit_normal)) > PLANAR_EPSILON:
            return False
    points = _project_face(vertices, face)
    if _has_self_intersections(points):
        return False
    sign = 0
    for index in range(len(points)):
        cross = _cross2(points[index - 1], points[index], points[(index + 1) % len(points)])
        if abs(cross) <= EPSILON:
            return False
        current_sign = 1 if cross > 0.0 else -1
        if sign == 0:
            sign = current_sign
        elif sign != current_sign:
            return False
    return True


def _has_self_intersections(points: Sequence[tuple[float, float]]) -> bool:
    count = len(points)
    for first in range(count):
        a0 = points[first]
        a1 = points[(first + 1) % count]
        for second in range(first + 1, count):
            if abs(first - second) <= 1 or {first, second} == {0, count - 1}:
                continue
            b0 = points[second]
            b1 = points[(second + 1) % count]
            if _segments_intersect(a0, a1, b0, b1):
                return True
    return False


def _segments_intersect(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> bool:
    def orient(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a0, a1, b0)
    o2 = orient(a0, a1, b1)
    o3 = orient(b0, b1, a0)
    o4 = orient(b0, b1, a1)
    return o1 * o2 < -EPSILON and o3 * o4 < -EPSILON


def _point_in_triangle(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    area = abs(_cross2(a, b, c))
    area1 = abs(_cross2(point, a, b))
    area2 = abs(_cross2(point, b, c))
    area3 = abs(_cross2(point, c, a))
    return abs(area - (area1 + area2 + area3)) <= 1.0e-8


def _oriented_triangle(first: int, second: int, third: int, was_reversed: bool) -> tuple[int, int, int]:
    if was_reversed:
        return (third, second, first)
    return (first, second, third)


def _quad_pair_score(
    vertices: Sequence[Vector3],
    quad: Sequence[int],
    guide_segments: Sequence[tuple[Vector3, Vector3]],
) -> float:
    aspect = _quad_aspect(vertices, quad)
    angle_penalty = _quad_angle_penalty(vertices, quad)
    guide_alignment = _guide_alignment(vertices, quad, guide_segments)
    return aspect + angle_penalty - 0.2 * guide_alignment


def _guide_alignment(
    vertices: Sequence[Vector3],
    quad: Sequence[int],
    guide_segments: Sequence[tuple[Vector3, Vector3]],
) -> float:
    if not guide_segments:
        return 0.0
    best = 0.0
    for first, second in _face_edges(quad):
        midpoint = _midpoint(vertices[first], vertices[second])
        edge_vector = _normalize(_sub(vertices[second], vertices[first]))
        closest_tangent = max(
            guide_segments,
            key=lambda item: -_distance_squared(midpoint, _closest_point_on_segment(midpoint, item[0], item[1])),
        )
        tangent = _normalize(_sub(closest_tangent[1], closest_tangent[0]))
        best = max(best, abs(_dot(edge_vector, tangent)))
    return best


def _guide_segments(guides: Sequence[GuideCurveData]) -> tuple[tuple[Vector3, Vector3], ...]:
    segments: list[tuple[Vector3, Vector3]] = []
    for guide in guides:
        for spline in guide.splines:
            for first, second in zip(spline, spline[1:]):
                if _distance_squared(first, second) > EPSILON:
                    segments.append((first, second))
                if len(segments) > MAX_GUIDE_SEGMENTS:
                    raise ValueError(f"가이드 세그먼트 수가 너무 많습니다: {len(segments)} > {MAX_GUIDE_SEGMENTS}")
    return tuple(segments)


def _quad_aspect_stats(vertices: Sequence[Vector3], faces: Sequence[Sequence[int]]) -> tuple[float, float]:
    if not faces:
        return 0.0, 0.0
    aspects = [_quad_aspect(vertices, face) for face in faces]
    return max(aspects), sum(aspects) / len(aspects)


def _quad_aspect(vertices: Sequence[Vector3], face: Sequence[int]) -> float:
    lengths = [_distance(vertices[first], vertices[second]) for first, second in _face_edges(face)]
    if any(length <= EPSILON for length in lengths):
        raise ValueError("쿼드 엣지 길이가 0이라 품질을 계산할 수 없습니다.")
    shortest = min(lengths)
    return max(lengths) / shortest


def _quad_angle_penalty(vertices: Sequence[Vector3], face: Sequence[int]) -> float:
    penalties = []
    for index, vertex_index in enumerate(face):
        previous_vertex = vertices[face[index - 1]]
        current_vertex = vertices[vertex_index]
        next_vertex = vertices[face[(index + 1) % len(face)]]
        incoming = _normalize(_sub(previous_vertex, current_vertex))
        outgoing = _normalize(_sub(next_vertex, current_vertex))
        penalties.append(_dot(incoming, outgoing) ** 2)
    return sum(penalties) / len(penalties)


def _face_edges(face: Sequence[int]) -> tuple[tuple[int, int], ...]:
    return tuple(zip(face, (*face[1:], face[0])))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _polygon_normal(vertices: Sequence[Vector3], face: Sequence[int]) -> Vector3:
    x = y = z = 0.0
    for first_index, second_index in _face_edges(face):
        first = vertices[first_index]
        second = vertices[second_index]
        x += (first[1] - second[1]) * (first[2] + second[2])
        y += (first[2] - second[2]) * (first[0] + second[0])
        z += (first[0] - second[0]) * (first[1] + second[1])
    return (x, y, z)


def _signed_area(points: Sequence[tuple[float, float]]) -> float:
    area = 0.0
    for first, second in zip(points, (*points[1:], points[0])):
        area += first[0] * second[1] - second[0] * first[1]
    return area * 0.5


def _is_convex_corner(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    return _cross2(a, b, c) > EPSILON


def _cross2(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])


def _dot(first: Vector3, second: Vector3) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _length(vector: Vector3) -> float:
    return sqrt(_dot(vector, vector))


def _normalize(vector: Vector3) -> Vector3:
    length = _length(vector)
    if length <= EPSILON:
        return (0.0, 0.0, 0.0)
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _scale(vector: Vector3, factor: float) -> Vector3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def _sub(first: Vector3, second: Vector3) -> Vector3:
    return (first[0] - second[0], first[1] - second[1], first[2] - second[2])


def _midpoint(first: Vector3, second: Vector3) -> Vector3:
    return ((first[0] + second[0]) * 0.5, (first[1] + second[1]) * 0.5, (first[2] + second[2]) * 0.5)


def _centroid(points: Sequence[Vector3]) -> Vector3:
    count = len(points)
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
        sum(point[2] for point in points) / count,
    )


def _lerp(first: Vector3, second: Vector3, ratio: float) -> Vector3:
    return (
        first[0] + (second[0] - first[0]) * ratio,
        first[1] + (second[1] - first[1]) * ratio,
        first[2] + (second[2] - first[2]) * ratio,
    )


def _bilinear(a: Vector3, b: Vector3, c: Vector3, d: Vector3, u: float, v: float) -> Vector3:
    bottom = _lerp(a, b, u)
    top = _lerp(d, c, u)
    return _lerp(bottom, top, v)


def _distance(first: Vector3, second: Vector3) -> float:
    return sqrt(_distance_squared(first, second))


def _distance_squared(first: Vector3, second: Vector3) -> float:
    return (
        (first[0] - second[0]) ** 2
        + (first[1] - second[1]) ** 2
        + (first[2] - second[2]) ** 2
    )


def _closest_point_on_segment(point: Vector3, start: Vector3, end: Vector3) -> Vector3:
    segment = _sub(end, start)
    length_squared = _dot(segment, segment)
    if length_squared <= EPSILON:
        return start
    ratio = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
    return _lerp(start, end, ratio)


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(fraction, message)


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("리메시 작업이 취소되었습니다.")
