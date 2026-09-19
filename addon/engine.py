from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from math import cos, sqrt
from statistics import median
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
FACE_NORMAL_EPSILON = 2.0e-12
PLANAR_EPSILON = 1.0e-6
MAX_SOURCE_VERTICES = 20000
MAX_SOURCE_FACES = 10000
MAX_SOURCE_CORNERS = 50000
MAX_FACE_VERTICES = 256
MAX_GUIDE_SEGMENTS = 20000
MAX_OUTPUT_QUADS = 200000
MAX_SURFACE_ERROR_RATIO = 0.04
MAX_ACCEPTED_ASPECT_RATIO = 20.0
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
    _quality_attempt: int = 0,
) -> RemeshResult:
    from .adaptive import adapt_triangles
    from .field import solve_field, optimize_quads
    from .surface import SurfaceIndex
    from .symmetry import clip_to_symmetry, mirror_symmetry

    settings = engine_input.settings
    settings.validate()
    engine_input.mesh.validate()
    _validate_size(engine_input.mesh)
    _check_cancelled(cancelled)
    _report(progress, 0.01, "입력과 특징선 검증")
    required_guides = tuple(
        guide.name
        for guide in engine_input.guide_curves
        if any(kind in {"LOOP", "STRIP"} for kind in guide.kind)
    )
    if required_guides and settings.topology_mode == "LEGACY":
        raise ValueError("실험 엔진은 필수 LOOP/STRIP 가이드를 보존하지 못합니다. 격자 경로를 선택해 주세요.")
    if settings.topology_mode != "LEGACY":
        structured = _try_structured_remesh(engine_input, progress=progress, cancelled=cancelled)
        if structured is not None:
            return structured
        if required_guides:
            raise ValueError(f"필수 가이드의 연속 엣지 경로를 만들지 못했습니다: {', '.join(required_guides)}. 현재 입력 형상에서 필수 루프와 쿼드 띠를 배치할 수 없습니다.")
        if settings.topology_mode == "STRUCTURED":
            raise ValueError("이 형상과 가이드에는 연속 격자 배치를 만들 수 없습니다. 격자 경계를 지정하거나 실험 엔진을 선택해 주세요.")
    source = engine_input.mesh
    scale = max(max(v[a] for v in source.vertices)-min(v[a] for v in source.vertices) for a in range(3))
    if scale <= 0:
        raise ValueError("입력 메시의 크기가 0입니다.")
    mesh = MeshData(tuple(_scale(v,1/scale) for v in source.vertices), source.faces, source.hard_edges)
    topology = _validate_topology(mesh, settings.hard_edge_angle_degrees)
    mesh = _triangulated(mesh, topology.feature_edges)
    density = engine_input.density_values
    if settings.symmetry_axes:
        mesh, density = clip_to_symmetry(mesh, density, settings.symmetry_axes)
        mesh = _triangulated(mesh, mesh.hard_edges)
        _validate_topology(mesh, 180.)
    reference = mesh
    surface = SurfaceIndex(reference)
    guides = tuple((_scale(a,1/scale),_scale(b,1/scale)) for a,b in _guide_segments(engine_input.guide_curves))
    # 대칭 축 중 표면 전체가 평면 위에 있는 축은 복제 배수에서 제외한다.
    axes = tuple(sorted(set(settings.symmetry_axes)))
    factor = 2**sum(any(abs(v['XYZ'.index(a)])>1e-9 for v in reference.vertices) for a in axes)
    capped_target = min(settings.target_quad_count, MAX_OUTPUT_QUADS)
    sector_target = max(1, round(capped_target/factor))
    warnings = []
    if settings.topology_mode == "AUTO":
        warnings.append("연속 격자 배치를 만들지 못해 실험 엔진을 사용했습니다. 엣지 루프 흐름을 확인하세요.")
    if capped_target != settings.target_quad_count:
        warnings.append(f"출력 상한 {MAX_OUTPUT_QUADS}쿼드에 맞춰 목표를 제한했습니다.")
    if axes:
        warnings.append("대칭은 오브젝트 로컬 원점의 양의 축 영역을 기준으로 생성했습니다.")
    best = None
    budget = max(1, round(sector_target/2.2))
    tried = set()
    alternatives = []
    for attempt in range(4):
        _check_cancelled(cancelled)
        if budget in tried:
            break
        tried.add(budget)
        _report(progress, .08+attempt*.12, f"목표 수와 밀도에 맞춰 재표본화 {attempt+1}/4")
        adapted, _, notes = adapt_triangles(reference, budget, density, density_scale=settings.density_scale, cancelled=cancelled,
            progress=lambda fraction,message: _report(progress,.08+attempt*.12+.10*fraction,message))
        adapted, _ = optimize_quads(adapted, surface, guides, iterations=0, cancelled=cancelled)
        adapted_topology = _validate_topology(adapted, 180.)
        field = solve_field(adapted, guides, cancelled=cancelled)
        patches = _build_patches(adapted, adapted_topology, guides, field)
        vertices, faces, hard = _patches_to_quads(adapted, patches, adapted.hard_edges)
        candidate = MeshData(tuple(vertices),tuple(faces),frozenset(hard))
        # 모든 삼각형이 사각형으로 짝지어졌으면 추가 분할 없는 출력도 비교한다.
        if all(len(p.vertices)==4 for p in patches):
            direct=MeshData(adapted.vertices,tuple(p.vertices for p in patches),adapted.hard_edges)
            alternatives.append(max(1,round(len(adapted.faces)*sector_target/len(direct.faces))))
            if abs(len(direct.faces)-sector_target)<abs(len(candidate.faces)-sector_target):
                candidate=direct
                faces=direct.faces
        elif any(len(p.vertices)==4 for p in patches):
            # 특징선으로 나뉜 영역의 홀수 삼각형 수를 바꿀 이웃 예산도 시도한다.
            alternatives.append(len(adapted.faces)+sum(len(p.vertices)==3 for p in patches))
        delta = abs(len(faces)-sector_target)
        rank = (len(faces)*factor > MAX_OUTPUT_QUADS, delta)
        if best is None or rank < best[0]:
            best = (rank,candidate,notes)
        if not rank[0] and delta <= sector_target*.015:
            break
        revised = max(1,round(len(adapted.faces)*sector_target/max(1,len(faces))))
        if revised == budget:
            revised += 1 if len(faces)<sector_target else -1
        budget = max(1,revised)
        if budget in tried:
            budget = next((item for item in alternatives if item not in tried),budget)
    if best is None:
        raise ValueError("목표에 맞는 쿼드 패치를 생성하지 못했습니다.")
    _, output, notes = best
    if notes:
        warnings.append("특징선과 경계 보존 제약으로 일부 면 개수 조정을 제한했습니다.")
    _report(progress,.64,"방향장 최적화와 원본 표면 재투영")
    output, field_score = optimize_quads(output,surface,guides,cancelled=cancelled)
    _validate_topology(output,180.)
    # 정점뿐 아니라 면 중심을 포함해 원본 삼각 표면과의 편차를 측정한다.
    distances=[]
    samples=list(output.vertices)+[_centroid([output.vertices[v] for v in f]) for f in output.faces]
    for i,p in enumerate(samples):
        if i%256==0:
            _check_cancelled(cancelled)
        distances.append(surface.nearest(p)[1]*scale)
    if max(distances,default=0.) > MAX_SURFACE_ERROR_RATIO*scale:
        if _quality_attempt < 2 and settings.target_quad_count < MAX_OUTPUT_QUADS:
            _report(progress,.82,"형상 보존을 위해 해상도 보정")
            refined_input=replace(engine_input,settings=replace(settings,target_quad_count=min(MAX_OUTPUT_QUADS,settings.target_quad_count*2)))
            refined=remesh(refined_input,progress=progress,cancelled=cancelled,_quality_attempt=_quality_attempt+1)
            refined_quality=replace(refined.quality,target_quad_count=settings.target_quad_count,
                target_error_ratio=abs(refined.quality.actual_quad_count-settings.target_quad_count)/settings.target_quad_count)
            refined_warnings=tuple(w for w in refined.warnings if "target=" not in w)
            refined_warnings += (f"표면 편차 제한을 지키기 위해 목표보다 해상도를 높였습니다: target={settings.target_quad_count}, actual={refined_quality.actual_quad_count}",)
            return replace(refined,quality=refined_quality,warnings=tuple(dict.fromkeys(refined_warnings)))
        raise ValueError("원본 대비 표면 편차가 크므로 안전한 결과를 만들지 못했습니다. 목표 쿼드 수를 높이거나 밀도 대비를 낮춰 주세요.")
    _report(progress,.86,"대칭 복원과 출력 검증")
    if axes:
        output=mirror_symmetry(output,axes)
    _validate_topology(output,180.)
    if len(output.faces)>MAX_OUTPUT_QUADS:
        raise ValueError("특징선과 대칭을 보존한 결과가 출력 상한을 초과했습니다. 목표 수를 낮춰 주세요.")
    analysis=analyze_mesh(output)
    if analysis.non_manifold_edge_count or analysis.degenerate_face_count or analysis.quad_ratio!=1:
        raise ValueError("출력 토폴로지 검증에 실패하여 결과 적용을 중단했습니다.")
    max_aspect,mean_aspect=_quad_aspect_stats(output.vertices,output.faces)
    if max_aspect > MAX_ACCEPTED_ASPECT_RATIO:
        raise ValueError(
            "결과에 지나치게 길고 좁은 쿼드가 있어 적용을 중단했습니다: "
            f"최대 종횡비 {max_aspect:.2f} > {MAX_ACCEPTED_ASPECT_RATIO:.0f}. "
            "격자 경계를 지정하거나 목표 쿼드 수를 조정해 주세요."
        )
    error=abs(analysis.quad_count-settings.target_quad_count)/settings.target_quad_count
    if analysis.quad_count!=settings.target_quad_count:
        warnings.append(f"위상·특징선·대칭 제약으로 목표와 실제 개수가 다릅니다: target={settings.target_quad_count}, actual={analysis.quad_count} ({error:.1%})")
    if max(distances,default=0.) > .02*scale:
        warnings.append("원본 대비 표면 편차가 크므로 목표 수를 높이거나 결과 형상을 확인하세요.")
    symmetry_error=_symmetry_error(output,axes)*scale
    output=MeshData(tuple(_scale(v,scale) for v in output.vertices),output.faces,output.hard_edges)
    _check_cancelled(cancelled)
    _report(progress,1.,"완료")
    return RemeshResult(output,RemeshQuality(
        settings.target_quad_count,analysis.quad_count,analysis.quad_ratio,
        analysis.boundary_edge_count,analysis.non_manifold_edge_count,analysis.degenerate_face_count,
        max_aspect,mean_aspect,error,max(distances,default=0.),sum(distances)/max(1,len(distances)),
        symmetry_error,field_score),tuple(dict.fromkeys(warnings)),())


def _try_structured_remesh(
    engine_input: EngineInput,
    *,
    progress: ProgressCallback | None,
    cancelled: CancelledCallback | None,
) -> RemeshResult | None:
    if engine_input.settings.target_quad_count > MAX_OUTPUT_QUADS:
        return None
    from .topology.quality import EdgePathExpectation, LayoutExpectations, measure_bidirectional_sample_distance, validate_layout
    from .topology.planar import try_remesh_planar
    from .topology.periodic import try_remesh_periodic
    from .topology.guided_surface import try_remesh_guided_surface

    source = engine_input.mesh
    _validate_topology(source, engine_input.settings.hard_edge_angle_degrees)
    for label, builder in (
        ("평면 격자", try_remesh_planar),
        ("주기 격자", try_remesh_periodic),
        ("가이드 곡면 격자", try_remesh_guided_surface),
    ):
        _check_cancelled(cancelled)
        _report(progress, 0.08, f"{label} 배치 탐색")
        output = builder(engine_input, cancelled=cancelled)
        if output is None:
            continue
        if len(output.faces) > MAX_OUTPUT_QUADS:
            raise ValueError(f"{label} 결과가 출력 상한 {MAX_OUTPUT_QUADS}쿼드를 초과했습니다.")
        _validate_topology(output, 180.0)
        analysis = analyze_mesh(output)
        if analysis.quad_ratio != 1.0 or analysis.non_manifold_edge_count or analysis.degenerate_face_count:
            raise ValueError(f"{label} 결과의 위상 검증에 실패했습니다.")
        output_edges = {_edge_key(first, second) for face in output.faces for first, second in _face_edges(face)}
        guide_tolerance = max(
            median(_distance(output.vertices[first], output.vertices[second]) for first, second in output_edges) * 0.75,
            1.0e-6,
        )
        loops = []
        strips = []
        for guide in engine_input.guide_curves:
            for index, (spline, kind) in enumerate(zip(guide.splines, guide.kind)):
                if kind not in {"LOOP", "STRIP"}:
                    continue
                expectation = EdgePathExpectation(
                    f"{guide.name}:{index}", tuple(spline),
                    closed=kind == "LOOP", tolerance=guide_tolerance,
                )
                (loops if kind == "LOOP" else strips).append(expectation)
        layout = validate_layout(output, LayoutExpectations(
            loops=tuple(loops), strips=tuple(strips),
            max_face_aspect_ratio=MAX_ACCEPTED_ASPECT_RATIO,
        ))
        if not layout.ok:
            raise ValueError(f"{label} 결과의 배치 품질 검사에 실패했습니다: {layout.issues[0].message}")
        # 경계 엣지 수는 분할 수에 따라 달라지므로 열린/닫힌 상태만 비교한다.
        if bool(analysis.boundary_edge_count) != bool(engine_input.analysis.boundary_edge_count):
            raise ValueError(f"{label} 결과의 열린 경계가 원본과 다릅니다.")
        _report(progress, 0.78, "양방향 원본 표면 오차 검사")
        surface_distance = measure_bidirectional_sample_distance(source, output, cancelled=cancelled)
        scale = max(max(v[a] for v in source.vertices) - min(v[a] for v in source.vertices) for a in range(3))
        maximum = surface_distance.max_distance
        if maximum > MAX_SURFACE_ERROR_RATIO * scale:
            raise ValueError(f"{label} 결과가 원본 표면에서 너무 멉니다: {maximum:.6g}")
        maximum_aspect, mean_aspect = _quad_aspect_stats(output.vertices, output.faces)
        if maximum_aspect > MAX_ACCEPTED_ASPECT_RATIO:
            raise ValueError(
                f"{label} 결과에 지나치게 길고 좁은 쿼드가 있습니다: "
                f"최대 종횡비 {maximum_aspect:.2f} > {MAX_ACCEPTED_ASPECT_RATIO:.0f}"
            )
        target = engine_input.settings.target_quad_count
        target_error = abs(analysis.quad_count - target) / target
        symmetry_error = _symmetry_error(output, tuple(sorted(set(engine_input.settings.symmetry_axes))))
        if symmetry_error > 1.0e-6 * scale:
            raise ValueError(f"{label} 결과가 요청한 대칭을 지키지 못했습니다.")
        warnings = []
        if analysis.quad_count != target:
            warnings.append(f"격자 분할 제약으로 목표와 실제 개수가 다릅니다: target={target}, actual={analysis.quad_count} ({target_error:.1%})")
        _report(progress, 1.0, f"{label} 완료")
        return RemeshResult(
            output,
            RemeshQuality(
                target, analysis.quad_count, analysis.quad_ratio,
                analysis.boundary_edge_count, analysis.non_manifold_edge_count,
                analysis.degenerate_face_count, maximum_aspect, mean_aspect,
                target_error, maximum,
                (surface_distance.mean_source_to_output + surface_distance.mean_output_to_source) / 2.0,
                symmetry_error, 1.0,
            ),
            tuple(warnings),
            (),
        )
    return None


def _triangulated(mesh, hard_edges):
    faces=[]
    for i,face in enumerate(mesh.faces):
        faces.extend([tuple(face)] if len(face)==3 else _ear_clip_face(mesh.vertices,face,i))
    return MeshData(mesh.vertices,tuple(faces),frozenset(hard_edges))


def _symmetry_error(mesh, axes):
    points={tuple(round(c,10) for c in p) for p in mesh.vertices}
    maximum=0.
    for axis in axes:
        a='XYZ'.index(axis)
        for p in mesh.vertices:
            mirrored=tuple(-c if i==a else c for i,c in enumerate(p))
            if tuple(round(c,10) for c in mirrored) not in points:
                maximum=max(maximum,min(_distance(mirrored,q) for q in mesh.vertices))
    return maximum


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
        normal_length = _length(normal)
        if normal_length <= FACE_NORMAL_EPSILON:
            raise ValueError(f"{face_index}번 면은 면적이 없어 퇴화되었습니다.")
        face_normals.append(_scale(normal, 1.0 / normal_length))

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
    field_directions: Sequence[Vector3] = (),
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

    paired_triangles = _pair_triangles(mesh, triangle_patches, topology.feature_edges, guide_segments, field_directions)
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
    field_directions: Sequence[Vector3] = (),
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
        if not _is_surface_convex(mesh.vertices, quad):
            continue
        score = _quad_pair_score(mesh.vertices, quad, guide_segments)
        if field_directions:
            from .field import alignment
            score -= 1.8*sum(alignment(mesh.vertices,quad,field_directions[triangle_patches[i][0]]) for i in (left,right))/2
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
    # 짧은 교대 경로로 탐욕적 매칭의 고립 삼각형을 줄인다.
    adjacency=defaultdict(list)
    for score,_,left,right in candidates:
        adjacency[left].append((score,right))
        adjacency[right].append((score,left))
    matches={i:(pair[1] if pair[0]==i else pair[0]) for i,pair in paired.items()}

    def augment(current, path):
        if len(path)>9:
            return None
        for _,neighbor in sorted(adjacency[current]):
            if neighbor in path or matches.get(current)==neighbor:
                continue
            if neighbor not in matches:
                return path+[neighbor]
            other=matches[neighbor]
            if other not in path:
                found=augment(other,path+[neighbor,other])
                if found:
                    return found
        return None

    for start in range(len(triangle_patches)):
        if start in matches:
            continue
        path=augment(start,[start])
        if path:
            for i in range(0,len(path),2):
                a,b=path[i:i+2]
                matches[a]=b
                matches[b]=a
    return {i:tuple(sorted((i,j))) for i,j in matches.items()}


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
    if _length(normal) <= FACE_NORMAL_EPSILON:
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
            if _length(normal) > FACE_NORMAL_EPSILON:
                return normal
    return (0.0, 0.0, 1.0)


def _is_surface_convex(vertices, face):
    normal=_normalize(_polygon_normal(vertices,face))
    if _length(normal)<EPSILON:
        return False
    for i,v in enumerate(face):
        a=_sub(vertices[v],vertices[face[i-1]])
        b=_sub(vertices[face[(i+1)%len(face)]],vertices[v])
        turn=(a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
        if _dot(turn,normal)<=EPSILON:
            return False
    return not _has_self_intersections(_project_face(vertices,face))


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
