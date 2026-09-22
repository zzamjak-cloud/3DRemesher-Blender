"""클릭 지점에서 팔·다리 단면 링을 추정하는 순수 기하 계산. bpy 없이 테스트한다.

축은 접평면 안의 방향 후보 중 단면 둘레가 가장 짧은 방향으로 고른다 — 관을 비스듬히 자르면 둘레가 길어지므로
최소 둘레 단면이 축에 수직이다. PCA 는 목처럼 길이가 폭보다 짧은 구간에서 폭 방향을 축으로 잡아 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, pi, sin, sqrt

from .core import MeshData, Vector3

HEMISPHERE_SAMPLES = 96    # 축 후보를 반구에 고르게 이만큼 뿌린다 (간격 약 11°)
REFINE_STEPS = (pi / 18, pi / 45, pi / 120)  # 최적 축 주변을 10°, 4°, 1.5° 로 좁혀 다듬는다
REFINE_AZIMUTHS = 8        # 다듬을 때 축 주위로 보는 방향 수
REFINE_MAX_ROUNDS = 12     # 단계마다 이동 반복 상한 — 점수는 단조 감소하지만 UI 정지 시간을 묶어 둔다
PLANE_NUDGE = 3.0          # 정점이 평면 위에 있을 때 평면을 PLANE_EPSILON 의 이 배수만큼 민다
HIT_OFFSET_MIN_RATIO = 0.5 # 클릭 점이 루프 중심에서 평균 반지름의 이 비율보다 가까우면 접선 조각 루프로 본다
MAX_NORMAL_ALIGNMENT = 0.7 # 루프를 따라 면 법선이 절단 축과 이보다 나란하면(≈45° 안) 관 단면이 아니라 돌출부 조각이다 (목은 어깨·턱으로 벌어져 0.5 까지 나온다)
STABILITY_SHIFT_RATIO = 0.15  # 축 점수를 매길 때 평면을 평균 반지름의 이 비율만큼 앞뒤로 옮겨 둘레 변화를 본다
STABILITY_SCALE_RATIO = 0.03  # 이동 폭 하한 = 모델 크기 × 이 비율 (작은 돌출부 조각은 이 폭 안에서 사라진다)
STABILITY_MIN_RATIO = 0.5  # 세 위치 둘레의 min/max 가 이보다 작으면 관 단면이 아니다
RESAMPLE_COUNT = 32
RIM_OK_RATIO = 0.5         # ring_cut.RIM_LENGTH_MIN_RATIO 와 같은 기준
PLANE_EPSILON = 1.0e-9


@dataclass(frozen=True)
class RingEstimate:
    center: Vector3
    axis: Vector3
    radius: float
    thickness: float
    loop: tuple[Vector3, ...]


class SectionMesh:
    """단면 계산용으로 미리 삼각화하고(가능하면 numpy 배열로) 준비한 메시. 클릭 한 번에 수백 번 자르므로 한 번만 만든다."""

    def __init__(self, mesh: MeshData):
        self.vertices = mesh.vertices
        self.triangles = [tri for face in mesh.faces for tri in _fan(face)]
        self.np = None
        try:
            import numpy as np
        except ImportError:  # 순수 파이썬 폴백 — 느리지만 같은 결과
            return
        self.np = np
        self.V = np.asarray(mesh.vertices, dtype=float)
        self.T = np.asarray(self.triangles, dtype=int)
        e1 = self.V[self.T[:, 1]] - self.V[self.T[:, 0]]
        e2 = self.V[self.T[:, 2]] - self.V[self.T[:, 0]]
        normals = np.cross(e1, e2)
        lengths = np.linalg.norm(normals, axis=1)
        lengths[lengths < 1.0e-30] = 1.0
        self.N = normals / lengths[:, None]


def section_loops(mesh, center, normal) -> list[tuple[list, bool, float]]:
    """평면과 메시의 교차 폴리라인들. (점 열, 닫힘, 면 법선과 평면 법선의 평균 |내적|) 목록.
    교차점은 엣지 키로 이어 붙여 좌표 오차에 흔들리지 않는다. 세 번째 값은 관을 수직으로 자른 단면이면 0 에 가깝고,
    턱·발등처럼 돌출부를 얇게 베어 낸 조각 루프면 1 에 가깝다. mesh 는 MeshData 나 SectionMesh."""
    section = mesh if isinstance(mesh, SectionMesh) else SectionMesh(mesh)
    points: dict = {}
    links: dict = {}
    alignment: dict = {}
    vertices = section.vertices
    if section.np is not None:
        np = section.np
        signed = (section.V - np.asarray(center, dtype=float)) @ np.asarray(normal, dtype=float)
        if np.any(np.abs(signed) < PLANE_EPSILON):
            signed = signed + PLANE_EPSILON * PLANE_NUDGE
        negative = signed < 0.0
        tri_negative = negative[section.T]
        crossing_index = np.nonzero(tri_negative.any(axis=1) & ~tri_negative.all(axis=1))[0]
        tri_alignment = np.abs(section.N[crossing_index] @ np.asarray(normal, dtype=float))
        candidates = ((section.triangles[i], float(tri_alignment[k])) for k, i in enumerate(crossing_index))
        signed = signed.tolist()
    else:
        signed = [(v[0] - center[0]) * normal[0] + (v[1] - center[1]) * normal[1] + (v[2] - center[2]) * normal[2] for v in vertices]
        if any(abs(d) < PLANE_EPSILON for d in signed):
            signed = [d + PLANE_EPSILON * PLANE_NUDGE for d in signed]
        candidates = ((tri, None) for tri in section.triangles)
    for tri, tri_align in candidates:
        crossing = []
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            da, db = signed[a], signed[b]
            if (da < 0.0) == (db < 0.0):
                continue
            key = (a, b) if a < b else (b, a)
            if key not in points:
                t = da / (da - db)
                va, vb = vertices[a], vertices[b]
                points[key] = (va[0] + (vb[0] - va[0]) * t, va[1] + (vb[1] - va[1]) * t, va[2] + (vb[2] - va[2]) * t)
            crossing.append(key)
        if len(crossing) == 2 and crossing[0] != crossing[1]:
            links.setdefault(crossing[0], []).append(crossing[1])
            links.setdefault(crossing[1], []).append(crossing[0])
            if tri_align is None:
                face_normal = _unit(_cross(
                    tuple(vertices[tri[1]][i] - vertices[tri[0]][i] for i in range(3)),
                    tuple(vertices[tri[2]][i] - vertices[tri[0]][i] for i in range(3)),
                ))
                tri_align = abs(_dot(face_normal, normal))
            alignment[frozenset(crossing)] = tri_align
    loops = []
    seen: set = set()
    for start in points:
        if start in seen or start not in links:
            continue
        component = _component(links, start)
        ends = [k for k in component if len(links[k]) == 1]
        origin = ends[0] if ends else start
        ordered = [origin]
        seen.add(origin)
        previous = None
        current = origin
        while True:
            following = [k for k in links[current] if k != previous and k not in seen]
            if not following:
                break
            previous, current = current, following[0]
            ordered.append(current)
            seen.add(current)
        seen |= component
        # 면 3장이 공유하는 엣지(비매니폴드)는 차수 4 접합점을 만들어 끝점이 없어도 되돌아오지 않는다 — 실제 폐합만 닫힘으로 본다
        closed = not ends and len(ordered) >= 3 and len(ordered) == len(component) and origin in links[ordered[-1]]
        pairs = list(zip(ordered, ordered[1:] + ([ordered[0]] if closed else [])))
        mean_alignment = sum(alignment.get(frozenset(pair), 0.0) for pair in pairs) / max(1, len(pairs))
        loops.append(([points[k] for k in ordered], closed, mean_alignment))
    return loops


def nearest_loop(loops, point):
    """정점이 point 에 가장 가까운 루프."""
    best = None
    best_distance = float("inf")
    for loop in loops:
        distance = min(_distance(p, point) for p in loop[0])
        if distance < best_distance:
            best, best_distance = loop, distance
    return best


def perimeter(points, closed: bool) -> float:
    count = len(points)
    if count < 2:
        return 0.0
    pairs = count if closed else count - 1
    return sum(_distance(points[i], points[(i + 1) % count]) for i in range(pairs))


def estimate_ring(mesh: MeshData, hit, normal, scale: float) -> RingEstimate | None:
    """클릭 점을 지나는 평면들 중 단면 루프 둘레가 가장 짧은 방향을 축으로 고른다.

    중심을 먼저 추정하지 않는다 — 표면 점에서 잰 현은 지름 방향이 가장 길고 축 방향으로는 무한히 길어져
    두께로 중심을 잡는 방식은 턱 아래처럼 법선이 기울어진 곳에서 머리 속으로 들어갔다(실측). 대신 평면이 클릭 점을
    지나게 하면 그 점을 포함하는 루프가 곧 단면이고, 루프 중심이 관 중심이다."""
    normal = _unit(normal)
    section = mesh if isinstance(mesh, SectionMesh) else SectionMesh(mesh)

    def measure(axis):
        loop = nearest_loop(section_loops(section, hit, axis), hit)
        if loop is None or not loop[1]:
            return float("inf"), None
        points = loop[0]
        centroid = tuple(sum(p[i] for p in points) / len(points) for i in range(3))
        distances = [_distance(p, centroid) for p in points]
        mean_radius = sum(distances) / len(points)
        # 표면에 거의 접하는 평면은 클릭 점 둘레의 작은 조각 루프를 만든다 — 클릭 점은 관 단면 위에 있으므로
        # 루프 중심에서 최소 반지름만큼은 떨어져 있어야 한다 (평균 반지름 기준은 납작한 팔뚝 단면을 기각했다)
        if mean_radius <= 0.0 or _distance(centroid, hit) < min(distances) * HIT_OFFSET_MIN_RATIO:
            return float("inf"), None
        # 턱·발등 같은 돌출부를 베어 낸 조각은 둘레가 짧아도 면 법선이 절단 축과 나란하다 — 관 단면은 법선이 축에 수직이다
        if loop[2] > MAX_NORMAL_ALIGNMENT:
            return float("inf"), None
        # 관 단면은 평면을 조금 옮겨도 둘레가 비슷하지만 턱 같은 돌출부 조각은 한쪽으로는 사라지고 다른 쪽으로는
        # 머리를 크게 벤다(실측: 0.45 → 0.02 / 열림). 세 위치가 모두 닫혀 있고 둘레가 절반 이상 비슷해야 하며,
        # 점수는 그중 최대 둘레다. 이동 폭은 반지름 비율과 모델 크기 비율 중 큰 쪽이다
        lengths = [perimeter(points, True)]
        shift = max(mean_radius * STABILITY_SHIFT_RATIO, scale * STABILITY_SCALE_RATIO)
        for sign in (-1.0, 1.0):
            shifted_center = tuple(hit[i] + axis[i] * shift * sign for i in range(3))
            shifted = nearest_loop(section_loops(section, shifted_center, axis), shifted_center)
            if shifted is None or not shifted[1]:
                return float("inf"), None
            lengths.append(perimeter(shifted[0], True))
        if min(lengths) < max(lengths) * STABILITY_MIN_RATIO:
            return float("inf"), None
        return max(lengths) * (1.0 + loop[2]), (loop, centroid, mean_radius)

    found = _search_axis(measure, normal)
    if found is None:
        return None
    axis, (loop, centroid, mean_radius) = found
    points = loop[0]
    radius = max(_distance(p, centroid) for p in points)
    return RingEstimate(centroid, axis, radius, mean_radius * 2.0, tuple(points))


def _search_axis(measure, normal):
    """반구 전체의 방향을 고르게 훑어 점수가 가장 낮은 축을 찾고, 그 주변을 단계적으로 좁혀 다듬는다.

    접평면 후보만으로 시작하면 턱 아래처럼 후보가 전부 기각되는 곳에서 탐색이 끊긴다(실측). 축은 부호가 없으므로
    반구만 본다."""
    best_axis, best_value, best_loop = None, float("inf"), None
    for axis in _hemisphere_directions(normal, HEMISPHERE_SAMPLES):
        value, loop = measure(axis)
        if value < best_value:
            best_axis, best_value, best_loop = axis, value, loop
    if best_loop is None:
        return None
    for step in REFINE_STEPS:
        improved = True
        rounds = 0
        while improved and rounds < REFINE_MAX_ROUNDS:
            improved = False
            rounds += 1
            t, b = _tangent_frame(best_axis)
            for k in range(REFINE_AZIMUTHS):
                angle = 2 * pi * k / REFINE_AZIMUTHS
                direction = _unit(tuple(
                    best_axis[i] * cos(step) + (t[i] * cos(angle) + b[i] * sin(angle)) * sin(step) for i in range(3)
                ))
                value, loop = measure(direction)
                if value < best_value:
                    best_axis, best_value, best_loop = direction, value, loop
                    improved = True
    return best_axis, best_loop


def _hemisphere_directions(pole, count: int):
    """pole 을 기준으로 한 반구를 피보나치 격자로 고르게 덮는 단위 벡터들."""
    t, b = _tangent_frame(pole)
    golden = pi * (3.0 - sqrt(5.0))
    directions = []
    for k in range(count):
        z = 1.0 - (k + 0.5) / count          # (0, 1]: pole 쪽 반구
        r = sqrt(max(0.0, 1.0 - z * z))
        angle = k * golden
        directions.append(_unit(tuple(pole[i] * z + (t[i] * cos(angle) + b[i] * sin(angle)) * r for i in range(3))))
    return directions


def slice_ring(mesh, center, axis, hit, radius: float = 0.0):
    """center 를 지나 axis 에 수직인 단면 중 hit 에 가장 가까운 닫힌 루프. 없으면 None. mesh 는 MeshData 나 SectionMesh."""
    del radius  # 예전 절단 반경 인자 — 전체 면을 자르므로 쓰지 않는다
    loop = nearest_loop(section_loops(mesh, center, axis), hit)
    if loop is None or not loop[1]:
        return None
    return tuple(loop[0])


def rim_ratio(mesh, center, axis, hit, radius: float, half_width: float) -> float:
    """center ± half_width 두 단면 둘레의 min/max. 발등·가슴을 함께 지나면 낮아진다."""
    mesh = mesh if isinstance(mesh, SectionMesh) else SectionMesh(mesh)
    lengths = []
    for sign in (-1.0, 1.0):
        shifted_center = tuple(center[i] + axis[i] * half_width * sign for i in range(3))
        shifted_hit = tuple(hit[i] + axis[i] * half_width * sign for i in range(3))
        loop = slice_ring(mesh, shifted_center, axis, shifted_hit, radius)
        if loop is None:
            return 0.0
        lengths.append(perimeter(loop, True))
    if max(lengths) <= 0.0:
        return 0.0
    return min(lengths) / max(lengths)


def resample_loop(points, count: int = RESAMPLE_COUNT) -> tuple[Vector3, ...]:
    """닫힌 폴리라인을 호 길이 기준 count 개로 다시 찍는다."""
    total = perimeter(points, True)
    if total <= 0.0 or len(points) < 3:
        return tuple(points)
    step = total / count
    result = []
    index = 0
    travelled = 0.0
    segment_start = points[0]
    segment_end = points[1 % len(points)]
    segment_length = _distance(segment_start, segment_end)
    target = 0.0
    while len(result) < count:
        while travelled + segment_length < target - 1.0e-12:
            travelled += segment_length
            index += 1
            segment_start = points[index % len(points)]
            segment_end = points[(index + 1) % len(points)]
            segment_length = _distance(segment_start, segment_end)
        ratio = 0.0 if segment_length <= 0.0 else (target - travelled) / segment_length
        result.append(tuple(segment_start[i] + (segment_end[i] - segment_start[i]) * ratio for i in range(3)))
        target += step
    return tuple(result)


# --- 보조 ------------------------------------------------------------------------

def _fan(face):
    if len(face) == 3:
        yield face
        return
    for index in range(1, len(face) - 1):
        yield (face[0], face[index], face[index + 1])


def _component(links: dict, start):
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for neighbor in links.get(current, ()):
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return seen


def _tangent_frame(normal):
    axis = min(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), key=lambda a: abs(_dot(a, normal)))
    t = _unit(tuple(axis[i] - normal[i] * _dot(axis, normal) for i in range(3)))
    return t, _cross(normal, t)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v):
    length = sqrt(_dot(v, v))
    return v if length < 1.0e-15 else (v[0] / length, v[1] / length, v[2] / length)


def _distance(a, b) -> float:
    return sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)
