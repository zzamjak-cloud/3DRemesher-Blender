from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite, sqrt
from typing import Callable, Iterable, Literal, Sequence


Vector3 = tuple[float, float, float]
GuideKind = Literal["LOOP", "STRIP", "DIRECTION"]
GUIDE_KIND_LOOP: GuideKind = "LOOP"
GUIDE_KIND_STRIP: GuideKind = "STRIP"
GUIDE_KIND_DIRECTION: GuideKind = "DIRECTION"
GUIDE_KIND_VALUES = (GUIDE_KIND_LOOP, GUIDE_KIND_STRIP, GUIDE_KIND_DIRECTION)
TOPOLOGY_MODES = ("AUTO", "STRUCTURED", "LEGACY")


@dataclass(frozen=True)
class RemeshSettings:
    target_quad_count: int = 5000
    symmetry_axes: tuple[str, ...] = field(default_factory=tuple)
    hard_edge_angle_degrees: float = 45.0
    guide_curve_names: tuple[str, ...] = field(default_factory=tuple)
    density_attribute_name: str = "remesh_density"
    density_scale: float = 1.0
    topology_mode: str = "AUTO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "topology_mode", str(self.topology_mode).strip().upper())

    def validate(self) -> None:
        if self.target_quad_count < 4:
            raise ValueError("목표 쿼드 수는 4 이상이어야 합니다.")
        if not self.density_attribute_name.strip():
            raise ValueError("밀도 속성 이름은 비어 있을 수 없습니다.")
        invalid_axes = sorted(set(self.symmetry_axes) - {"X", "Y", "Z"})
        if invalid_axes:
            raise ValueError(f"지원하지 않는 대칭 축입니다: {', '.join(invalid_axes)}")
        if not 0.0 <= self.hard_edge_angle_degrees <= 180.0:
            raise ValueError("하드 엣지 각도는 0도에서 180도 사이여야 합니다.")
        if not isfinite(self.density_scale) or self.density_scale <= 0.0:
            raise ValueError("밀도 배율은 0보다 커야 합니다.")
        if self.topology_mode not in TOPOLOGY_MODES:
            raise ValueError(f"지원하지 않는 위상 모드입니다: {self.topology_mode}")


@dataclass(frozen=True)
class MeshData:
    vertices: tuple[Vector3, ...]
    faces: tuple[tuple[int, ...], ...]
    hard_edges: frozenset[tuple[int, int]] = field(default_factory=frozenset)

    def validate(self) -> None:
        if len(self.vertices) < 3:
            raise ValueError("메시는 최소 3개의 정점이 필요합니다.")
        if not self.faces:
            raise ValueError("메시는 최소 1개의 면이 필요합니다.")
        vertex_count = len(self.vertices)
        for vertex_index, vertex in enumerate(self.vertices):
            _validate_vector3(vertex, f"{vertex_index}번 정점")
        for face_index, face in enumerate(self.faces):
            if len(face) < 3:
                raise ValueError(f"{face_index}번 면은 정점이 3개 미만입니다.")
            for vertex_index in face:
                if vertex_index < 0 or vertex_index >= vertex_count:
                    raise ValueError(f"{face_index}번 면이 잘못된 정점 인덱스를 참조합니다.")
        for edge in self.hard_edges:
            if len(edge) != 2:
                raise ValueError("하드 엣지는 정점 인덱스 2개가 필요합니다.")
            first, second = edge
            if first == second:
                raise ValueError("하드 엣지는 서로 다른 정점 2개가 필요합니다.")
            if first < 0 or second < 0 or first >= vertex_count or second >= vertex_count:
                raise ValueError("하드 엣지가 잘못된 정점 인덱스를 참조합니다.")


@dataclass(frozen=True)
class MeshAnalysis:
    vertex_count: int
    edge_count: int
    face_count: int
    triangle_count: int
    quad_count: int
    n_gon_count: int
    boundary_edge_count: int
    non_manifold_edge_count: int
    degenerate_face_count: int

    @property
    def quad_ratio(self) -> float:
        if self.face_count == 0:
            return 0.0
        return self.quad_count / self.face_count

    def summary_ko(self) -> str:
        return (
            f"정점 {self.vertex_count}, 엣지 {self.edge_count}, 면 {self.face_count}, "
            f"쿼드 {self.quad_count} ({self.quad_ratio:.1%}), "
            f"경계 엣지 {self.boundary_edge_count}, 비다양체 엣지 {self.non_manifold_edge_count}, "
            f"퇴화 면 {self.degenerate_face_count}"
        )


@dataclass(frozen=True)
class GuideCurveData:
    name: str
    splines: tuple[tuple[Vector3, ...], ...]
    kind: tuple[str, ...] = field(default_factory=tuple)
    closed: tuple[bool, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        spline_count = len(self.splines)
        raw_closed = tuple(bool(value) for value in self.closed)
        raw_kind = tuple(str(value).strip().upper() for value in self.kind)
        if raw_closed and len(raw_closed) != spline_count:
            raise ValueError(f"{self.name} 가이드 closed 메타데이터 수가 스플라인 수와 다릅니다.")
        if raw_kind and len(raw_kind) != spline_count:
            raise ValueError(f"{self.name} 가이드 kind 메타데이터 수가 스플라인 수와 다릅니다.")
        closed = raw_closed or tuple(value == GUIDE_KIND_LOOP for value in raw_kind) or (False,) * spline_count
        kind = raw_kind or tuple(GUIDE_KIND_LOOP if value else GUIDE_KIND_DIRECTION for value in closed)
        invalid_kind = sorted(set(kind) - set(GUIDE_KIND_VALUES))
        if invalid_kind:
            raise ValueError(f"{self.name} 가이드 kind가 지원되지 않습니다: {', '.join(invalid_kind)}")
        for index, (guide_kind, is_closed) in enumerate(zip(kind, closed)):
            if guide_kind == GUIDE_KIND_LOOP and not is_closed:
                raise ValueError(f"{self.name} {index}번 LOOP 가이드는 닫힌 스플라인이어야 합니다.")
            if guide_kind in {GUIDE_KIND_STRIP, GUIDE_KIND_DIRECTION} and is_closed:
                raise ValueError(f"{self.name} {index}번 {guide_kind} 가이드는 열린 스플라인이어야 합니다.")
        object.__setattr__(self, "kind", tuple(kind))
        object.__setattr__(self, "closed", tuple(closed))

    @property
    def points(self) -> tuple[Vector3, ...]:
        return tuple(point for spline in self.splines for point in spline)


@dataclass(frozen=True)
class EngineInput:
    mesh: MeshData
    settings: RemeshSettings
    analysis: MeshAnalysis
    guide_curves: tuple[GuideCurveData, ...] = field(default_factory=tuple)
    density_values: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RemeshQuality:
    target_quad_count: int
    actual_quad_count: int
    quad_ratio: float
    boundary_edge_count: int
    non_manifold_edge_count: int
    degenerate_face_count: int
    max_aspect_ratio: float
    mean_aspect_ratio: float
    target_error_ratio: float = 0.0
    max_surface_error: float = 0.0
    mean_surface_error: float = 0.0
    symmetry_error: float = 0.0
    field_alignment: float = 0.0

    def summary_ko(self) -> str:
        return (
            f"목표 쿼드 {self.target_quad_count}, 실제 쿼드 {self.actual_quad_count}, "
            f"쿼드 비율 {self.quad_ratio:.1%}, 경계 엣지 {self.boundary_edge_count}, "
            f"비다양체 엣지 {self.non_manifold_edge_count}, 퇴화 면 {self.degenerate_face_count}, "
            f"최대 종횡비 {self.max_aspect_ratio:.2f}, 평균 종횡비 {self.mean_aspect_ratio:.2f}"
        )


@dataclass(frozen=True)
class RemeshResult:
    mesh: MeshData
    quality: RemeshQuality
    warnings: tuple[str, ...] = field(default_factory=tuple)
    unsupported_controls: tuple[str, ...] = field(default_factory=tuple)

    def summary_ko(self) -> str:
        suffix = ""
        if self.warnings:
            suffix = f" 경고 {len(self.warnings)}개."
        if self.unsupported_controls:
            suffix += f" 미지원 제어: {', '.join(self.unsupported_controls)}."
        return f"{self.quality.summary_ko()}.{suffix}"


class RemeshCancelled(RuntimeError):
    pass


ProgressCallback = Callable[[float, str], None]
CancelledCallback = Callable[[], bool]


def analyze_mesh(mesh: MeshData) -> MeshAnalysis:
    mesh.validate()
    edge_face_counts = _edge_face_counts(mesh.faces)
    degenerate_faces = sum(1 for face in mesh.faces if _is_degenerate_face(mesh.vertices, face))
    triangle_count = sum(1 for face in mesh.faces if len(face) == 3)
    quad_count = sum(1 for face in mesh.faces if len(face) == 4)
    n_gon_count = sum(1 for face in mesh.faces if len(face) > 4)
    boundary_edge_count = sum(1 for count in edge_face_counts.values() if count == 1)
    non_manifold_edge_count = sum(1 for count in edge_face_counts.values() if count > 2)

    return MeshAnalysis(
        vertex_count=len(mesh.vertices),
        edge_count=len(edge_face_counts),
        face_count=len(mesh.faces),
        triangle_count=triangle_count,
        quad_count=quad_count,
        n_gon_count=n_gon_count,
        boundary_edge_count=boundary_edge_count,
        non_manifold_edge_count=non_manifold_edge_count,
        degenerate_face_count=degenerate_faces,
    )


def build_engine_input(
    mesh: MeshData,
    settings: RemeshSettings,
    guide_curves: Iterable[GuideCurveData] = (),
    density_values: Sequence[float] = (),
) -> EngineInput:
    settings.validate()
    mesh.validate()
    density_tuple = tuple(float(value) for value in density_values)
    if density_tuple and len(density_tuple) != len(mesh.vertices):
        raise ValueError("밀도 값 개수는 메시 정점 개수와 같아야 합니다.")
    for index, value in enumerate(density_tuple):
        if not isfinite(value) or value < 0:
            raise ValueError(f"{index}번 밀도 값은 0 이상의 유한한 숫자여야 합니다.")

    guide_tuple = tuple(guide_curves)
    for guide in guide_tuple:
        for spline_index, spline in enumerate(guide.splines):
            for point_index, point in enumerate(spline):
                _validate_vector3(point, f"{guide.name} {spline_index}번 스플라인 {point_index}번 점")

    return EngineInput(
        mesh=mesh,
        settings=settings,
        analysis=analyze_mesh(mesh),
        guide_curves=guide_tuple,
        density_values=density_tuple,
    )


class RemeshBackend:
    def build_input(
        self,
        mesh: MeshData,
        settings: RemeshSettings,
        guide_curves: Iterable[GuideCurveData] = (),
        density_values: Sequence[float] = (),
    ) -> EngineInput:
        return build_engine_input(mesh, settings, guide_curves, density_values)

    def remesh(
        self,
        engine_input: EngineInput,
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancelledCallback | None = None,
    ) -> RemeshResult:
        from .engine import remesh

        return remesh(engine_input, progress=progress, cancelled=cancelled)


def _edge_face_counts(faces: Iterable[Sequence[int]]) -> dict[tuple[int, int], int]:
    edge_counts: dict[tuple[int, int], int] = {}
    for face in faces:
        for first, second in zip(face, (*face[1:], face[0])):
            edge = tuple(sorted((first, second)))
            edge_counts[edge] = edge_counts.get(edge, 0) + 1
    return edge_counts


def _validate_vector3(value: Sequence[float], label: str) -> None:
    if len(value) != 3:
        raise ValueError(f"{label} 좌표는 3개 값이 필요합니다.")
    for axis, component in zip(("X", "Y", "Z"), value):
        if not isfinite(float(component)):
            raise ValueError(f"{label} {axis} 좌표는 유한한 숫자여야 합니다.")


def _is_degenerate_face(vertices: Sequence[Vector3], face: Sequence[int]) -> bool:
    if len(set(face)) != len(face):
        return True
    first = vertices[face[0]]
    for index in range(1, len(face) - 1):
        area = _triangle_area(first, vertices[face[index]], vertices[face[index + 1]])
        if area > 1.0e-12:
            return False
    return True


def _triangle_area(a: Vector3, b: Vector3, c: Vector3) -> float:
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return 0.5 * sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2)
