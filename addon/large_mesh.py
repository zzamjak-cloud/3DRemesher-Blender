from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from math import isfinite, sqrt
from typing import Sequence

from .core import (
    EngineInput,
    MeshData,
    ProgressCallback,
    CancelledCallback,
    RemeshBackend,
    RemeshCancelled,
    RemeshQuality,
    RemeshResult,
    Vector3,
    analyze_mesh,
    build_engine_input,
    _is_degenerate_face,
)
from .engine import (
    MAX_ACCEPTED_ASPECT_RATIO,
    MAX_SOURCE_CORNERS,
    MAX_SOURCE_FACES,
    MAX_SOURCE_VERTICES,
    _try_structured_remesh,
    check_guide_gate,
    required_guide_names,
    try_quadriflow_remesh,
)


MAX_ENGINE_TRIANGLES = 20000
MAX_PROXY_TRIANGLES = 4000
MAX_SURFACE_ERROR_RATIO = 0.04


def needs_preprocessing(mesh: MeshData) -> bool:
    """bpy 없이 큰 메시 우회 경로가 필요한지 판단한다."""
    corner_count = sum(len(face) for face in mesh.faces)
    triangle_count = sum(max(1, len(face) - 2) for face in mesh.faces)
    return (
        len(mesh.vertices) > MAX_SOURCE_VERTICES
        or len(mesh.faces) > MAX_SOURCE_FACES
        or corner_count > MAX_SOURCE_CORNERS
        or triangle_count > MAX_ENGINE_TRIANGLES
    )


def remesh_large(
    engine_input: EngineInput,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelledCallback | None = None,
) -> RemeshResult:
    _check_cancelled(cancelled)
    if not needs_preprocessing(engine_input.mesh):
        return RemeshBackend().remesh(engine_input, progress=progress, cancelled=cancelled)

    settings = engine_input.settings
    required_guides, strip_guides = required_guide_names(engine_input)
    check_guide_gate(required_guides, strip_guides, settings.topology_mode)
    structured_warning = ""
    if settings.topology_mode in {"AUTO", "STRUCTURED"}:
        try:
            structured = _try_structured_remesh(engine_input, progress=progress, cancelled=cancelled)
        except ValueError as exc:
            if strip_guides or settings.topology_mode == "STRUCTURED":
                raise
            structured = None
            structured_warning = f"원본 메시의 격자 배치를 건너뛰고 임시 프록시를 사용했습니다: {exc}"
        if structured is not None:
            return structured
        if strip_guides or (required_guides and settings.topology_mode == "STRUCTURED"):
            raise ValueError(f"필수 가이드의 연속 엣지 경로를 만들지 못했습니다: {', '.join(required_guides)}. 현재 입력 형상에서 필수 루프와 쿼드 띠를 배치할 수 없습니다.")
        if settings.topology_mode == "STRUCTURED":
            raise ValueError("이 형상과 가이드에는 연속 격자 배치를 만들 수 없습니다. 격자 경계를 지정하거나 실험 엔진을 선택해 주세요.")
    quadriflow_warning = ""
    if settings.topology_mode in {"AUTO", "QUADRIFLOW"}:
        general, quadriflow_warning = try_quadriflow_remesh(engine_input, progress=progress, cancelled=cancelled)
        if general is not None:
            return general
        if settings.topology_mode == "QUADRIFLOW":
            raise ValueError(quadriflow_warning or "QuadriFlow 경로가 결과를 만들지 못했습니다.")
        if required_guides:
            raise ValueError(
                f"필수 가이드의 연속 엣지 경로를 만들지 못했습니다: {', '.join(required_guides)}. "
                f"격자 경로가 실패했고 QuadriFlow 절단 링도 쓸 수 없습니다. {quadriflow_warning}".rstrip()
            )

    _require_blender()
    try:
        return _remesh_with_proxy(engine_input, structured_warning, quadriflow_warning, progress=progress, cancelled=cancelled)
    except ValueError as exc:
        if quadriflow_warning:
            raise ValueError(f"{exc} (앞선 QuadriFlow 경로: {quadriflow_warning})") from exc
        raise


def _remesh_with_proxy(
    engine_input: EngineInput,
    structured_warning: str,
    quadriflow_warning: str,
    *,
    progress: ProgressCallback | None,
    cancelled: CancelledCallback | None,
) -> RemeshResult:
    """격자·QuadriFlow 경로가 모두 실패한 큰 입력을 임시 Decimate 프록시와 자체 엔진으로 처리한다."""
    _report(progress, 0.01, "큰 메시 전처리 준비")
    source = engine_input.mesh
    source.validate()
    source_analysis = analyze_mesh(source)
    source_scale = _bbox_scale(source.vertices)
    if source_scale <= 0.0:
        raise ValueError("입력 메시의 크기가 0입니다.")

    _check_cancelled(cancelled)
    cleaned, cleanup_warnings = _clean_for_proxy(source)
    _validate_cleaned_topology(cleaned, engine_input.settings.hard_edge_angle_degrees)
    surface = _OriginalSurface(source)
    proxy_triangle_target = _proxy_triangle_target(cleaned)

    _report(progress, 0.07, f"임시 메시를 {proxy_triangle_target}개 이하 삼각형 프록시로 축소")
    proxy_mesh, proxy_notes = _build_decimated_proxy(
        cleaned,
        proxy_triangle_target,
        progress=lambda fraction, message: _report(progress, 0.07 + fraction * 0.23, message),
        cancelled=cancelled,
    )
    proxy_mesh, proxy_cleanup_warnings = _clean_for_proxy(proxy_mesh)
    _validate_cleaned_topology(proxy_mesh, engine_input.settings.hard_edge_angle_degrees)
    proxy_analysis = analyze_mesh(proxy_mesh)
    if proxy_analysis.triangle_count > MAX_PROXY_TRIANGLES:
        raise ValueError(
            f"큰 메시 프록시가 처리 예산을 초과했습니다: "
            f"{proxy_analysis.triangle_count} > {MAX_PROXY_TRIANGLES}"
        )
    _verify_boundary_survival(source_analysis, proxy_analysis, "프록시")
    _verify_explicit_features(source, proxy_mesh, "프록시")

    _check_cancelled(cancelled)
    proxy_density = surface.sample_density(proxy_mesh.vertices, engine_input.density_values, cancelled=cancelled)
    proxy_input = build_engine_input(
        proxy_mesh,
        engine_input.settings,
        engine_input.guide_curves,
        proxy_density,
    )

    _report(progress, 0.32, "프록시 메시 리메시")
    proxy_result = RemeshBackend().remesh(
        proxy_input,
        progress=lambda fraction, message: _report(progress, 0.32 + fraction * 0.43, message),
        cancelled=cancelled,
    )

    _check_cancelled(cancelled)
    _report(progress, 0.78, "원본 표면 기준 품질 검사")
    output_mesh = proxy_result.mesh
    output_mesh.validate()

    max_error, mean_error = surface.surface_error(output_mesh, cancelled=cancelled)
    if max_error > MAX_SURFACE_ERROR_RATIO * source_scale:
        raise ValueError(
            "원본 대비 표면 편차가 크므로 안전한 결과를 만들지 못했습니다. "
            "목표 쿼드 수를 높이거나 밀도 대비를 낮춰 주세요."
        )

    analysis = analyze_mesh(output_mesh)
    if analysis.non_manifold_edge_count or analysis.degenerate_face_count or analysis.quad_ratio != 1:
        raise ValueError("큰 메시 결과 토폴로지 검증에 실패하여 결과 적용을 중단했습니다.")
    _verify_boundary_survival(source_analysis, analysis, "최종 결과")
    _verify_explicit_features(source, output_mesh, "최종 결과")

    max_aspect, mean_aspect = _quad_aspect_stats(output_mesh.vertices, output_mesh.faces)
    if max_aspect > MAX_ACCEPTED_ASPECT_RATIO:
        raise ValueError(
            "큰 메시 결과에 지나치게 길고 좁은 쿼드가 있어 적용을 중단했습니다: "
            f"최대 종횡비 {max_aspect:.2f} > {MAX_ACCEPTED_ASPECT_RATIO:.0f}. "
            "격자 경계를 지정하거나 목표 쿼드 수를 조정해 주세요."
        )
    target = engine_input.settings.target_quad_count
    target_error = abs(analysis.quad_count - target) / max(1, target)
    symmetry_error = proxy_result.quality.symmetry_error
    quality = RemeshQuality(
        target_quad_count=target,
        actual_quad_count=analysis.quad_count,
        quad_ratio=analysis.quad_ratio,
        boundary_edge_count=analysis.boundary_edge_count,
        non_manifold_edge_count=analysis.non_manifold_edge_count,
        degenerate_face_count=analysis.degenerate_face_count,
        max_aspect_ratio=max_aspect,
        mean_aspect_ratio=mean_aspect,
        target_error_ratio=target_error,
        max_surface_error=max_error,
        mean_surface_error=mean_error,
        symmetry_error=symmetry_error,
        field_alignment=proxy_result.quality.field_alignment,
    )

    warnings = (
        "큰 메시 경로: 원본은 변경하지 않고 임시 프록시에서 리메시했습니다.",
        structured_warning,
        quadriflow_warning,
        *cleanup_warnings,
        *proxy_cleanup_warnings,
        *proxy_notes,
        *proxy_result.warnings,
    )
    _report(progress, 1.0, "완료")
    return replace(
        proxy_result,
        mesh=output_mesh,
        quality=quality,
        warnings=tuple(dict.fromkeys(warning for warning in warnings if warning)),
    )


def _require_blender() -> None:
    try:
        import bpy  # noqa: F401
        import bmesh  # noqa: F401
        import mathutils  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError("큰 메시 전처리는 Blender Python 환경에서 실행해야 합니다.") from exc


def _clean_for_proxy(mesh: MeshData) -> tuple[MeshData, tuple[str, ...]]:
    vertices = list(mesh.vertices)
    faces: list[tuple[int, ...]] = []
    warnings: list[str] = []
    seen_faces: set[tuple[int, ...]] = set()
    removed_duplicate_faces = 0
    removed_degenerate_faces = 0

    for face in mesh.faces:
        # 정점이 겹치거나 면적이 없는 면은 프록시 검증을 통과하지 못하므로 미리 뺀다 (실측: 갱스터 10만면에 23개)
        if len(set(face)) != len(face) or _is_degenerate_face(vertices, face):
            removed_degenerate_faces += 1
            continue
        key = tuple(sorted(face))
        if key in seen_faces:
            removed_duplicate_faces += 1
            continue
        seen_faces.add(key)
        faces.append(tuple(face))

    if removed_duplicate_faces:
        warnings.append(f"임시 프록시에서 중복 면 {removed_duplicate_faces}개를 정리했습니다.")
    if removed_degenerate_faces:
        warnings.append(f"임시 프록시에서 퇴화 면 {removed_degenerate_faces}개를 제외했습니다.")

    edge_faces: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for face_index, face in enumerate(faces):
        for first, second in _face_edges(face):
            edge = _edge_key(first, second)
            edge_faces[edge].append((face_index, first, second))

    face_duplicate_vertices: dict[int, set[int]] = defaultdict(set)
    split_non_manifold_edges = 0
    split_winding_edges = 0
    for edge, uses in edge_faces.items():
        if len(uses) > 2:
            split_non_manifold_edges += 1
            for face_index, first, second in uses[2:]:
                face_duplicate_vertices[face_index].update((first, second))
        elif len(uses) == 2 and (uses[0][1], uses[0][2]) == (uses[1][1], uses[1][2]):
            split_winding_edges += 1
            face_duplicate_vertices[uses[1][0]].update(edge)

    if split_non_manifold_edges:
        warnings.append(f"임시 프록시에서 비다양체 엣지 {split_non_manifold_edges}개를 분리했습니다.")
    if split_winding_edges:
        warnings.append(f"임시 프록시에서 winding 충돌 엣지 {split_winding_edges}개를 분리했습니다.")

    remapped_faces = list(faces)
    duplicate_map: dict[tuple[int, int], int] = {}
    for face_index, source_vertices in face_duplicate_vertices.items():
        face = remapped_faces[face_index]
        remapped = []
        for vertex_index in face:
            if vertex_index in source_vertices:
                key = (face_index, vertex_index)
                if key not in duplicate_map:
                    duplicate_map[key] = len(vertices)
                    vertices.append(vertices[vertex_index])
                remapped.append(duplicate_map[key])
            else:
                remapped.append(vertex_index)
        remapped_faces[face_index] = tuple(remapped)

    bowtie_splits = _split_bowtie_vertices(vertices, remapped_faces)
    if bowtie_splits:
        warnings.append(f"임시 프록시에서 bowtie 정점 컴포넌트 {bowtie_splits}개를 분리했습니다.")

    hard_edges = _remap_hard_edges(mesh.hard_edges, faces, remapped_faces)
    return MeshData(tuple(vertices), tuple(remapped_faces), frozenset(hard_edges)), tuple(warnings)


def _split_bowtie_vertices(vertices: list[Vector3], faces: list[tuple[int, ...]]) -> int:
    split_count = 0
    while True:
        components_by_vertex = _bowtie_components(faces)
        if not components_by_vertex:
            return split_count
        for vertex_index, components in components_by_vertex.items():
            for component in components[1:]:
                duplicate_index = len(vertices)
                vertices.append(vertices[vertex_index])
                for face_index in component:
                    face = faces[face_index]
                    faces[face_index] = tuple(duplicate_index if index == vertex_index else index for index in face)
                split_count += 1


def _bowtie_components(faces: Sequence[tuple[int, ...]]) -> dict[int, list[set[int]]]:
    incident_faces: dict[int, set[int]] = defaultdict(set)
    connected_faces: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    edge_to_faces: dict[tuple[int, int], list[int]] = defaultdict(list)

    for face_index, face in enumerate(faces):
        for vertex in face:
            incident_faces[vertex].add(face_index)
            connected_faces[vertex][face_index].add(face_index)
        for first, second in _face_edges(face):
            edge_to_faces[_edge_key(first, second)].append(face_index)

    for (first, second), edge_faces in edge_to_faces.items():
        for face_index in edge_faces:
            connected_faces[first][face_index].update(edge_faces)
            connected_faces[second][face_index].update(edge_faces)

    output: dict[int, list[set[int]]] = {}
    for vertex, face_set in incident_faces.items():
        if len(face_set) <= 1:
            continue
        remaining = set(face_set)
        components = []
        while remaining:
            start = next(iter(remaining))
            component = {start}
            stack = [start]
            remaining.remove(start)
            while stack:
                face_index = stack.pop()
                for neighbor in connected_faces[vertex][face_index]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        stack.append(neighbor)
            components.append(component)
        if len(components) > 1:
            output[vertex] = components
    return output


def _validate_cleaned_topology(mesh: MeshData, hard_edge_angle_degrees: float) -> None:
    from .engine import _validate_topology

    try:
        _validate_topology(mesh, hard_edge_angle_degrees)
    except ValueError as exc:
        raise ValueError(f"큰 메시 임시 정리 후 토폴로지 검증에 실패했습니다: {exc}") from exc


def _remap_hard_edges(
    hard_edges: frozenset[tuple[int, int]],
    original_faces: Sequence[tuple[int, ...]],
    remapped_faces: Sequence[tuple[int, ...]],
) -> set[tuple[int, int]]:
    hard = {_edge_key(*edge) for edge in hard_edges}
    output = set(hard)
    if not hard:
        return output
    for original, remapped in zip(original_faces, remapped_faces):
        for (first, second), (new_first, new_second) in zip(_face_edges(original), _face_edges(remapped)):
            if _edge_key(first, second) in hard and new_first != new_second:
                output.add(_edge_key(new_first, new_second))
    return output


def _build_decimated_proxy(
    mesh: MeshData,
    target_triangles: int,
    *,
    progress: ProgressCallback | None,
    cancelled: CancelledCallback | None,
) -> tuple[MeshData, tuple[str, ...]]:
    import bpy

    _check_cancelled(cancelled)
    temp_mesh = bpy.data.meshes.new("ZZ_LargeMeshProxySource")
    temp_obj = None
    evaluated_mesh = None
    notes: list[str] = []
    try:
        temp_mesh.from_pydata(mesh.vertices, [], mesh.faces)
        temp_mesh.update(calc_edges=True)
        temp_mesh.calc_loop_triangles()
        if temp_mesh.validate(clean_customdata=False):
            raise ValueError("임시 프록시 입력이 Blender 검증을 통과하지 못했습니다.")
        _mark_feature_edges(temp_mesh, mesh.hard_edges)
        temp_obj = bpy.data.objects.new("ZZ_LargeMeshProxySource", temp_mesh)
        bpy.context.scene.collection.objects.link(temp_obj)
        _assign_protected_vertices(temp_obj, mesh)
        bpy.context.view_layer.objects.active = temp_obj
        temp_obj.select_set(True)
        bpy.context.view_layer.update()

        source_triangles = sum(max(1, len(face) - 2) for face in mesh.faces)
        ratio = min(1.0, max(1.0 / max(1, source_triangles), target_triangles / max(1, source_triangles)))
        modifier = temp_obj.modifiers.new("ZZ_LargeMeshDecimate", "DECIMATE")
        modifier.ratio = ratio
        if temp_obj.vertex_groups:
            modifier.vertex_group = temp_obj.vertex_groups[0].name
            if hasattr(modifier, "invert_vertex_group"):
                modifier.invert_vertex_group = True
            if hasattr(modifier, "vertex_group_factor"):
                modifier.vertex_group_factor = 1.0
        if hasattr(modifier, "use_collapse_triangulate"):
            modifier.use_collapse_triangulate = True
        if hasattr(modifier, "use_dissolve_boundaries"):
            modifier.use_dissolve_boundaries = False

        _report(progress, 0.35, "Blender Decimate 적용")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_obj = temp_obj.evaluated_get(depsgraph)
        evaluated_mesh = evaluated_obj.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        proxy = _mesh_data_from_blender_mesh(evaluated_mesh, triangulate=True)
        if analyze_mesh(proxy).triangle_count > target_triangles:
            notes.append(
                f"프록시 삼각형 수가 목표보다 큽니다: target={target_triangles}, actual={analyze_mesh(proxy).triangle_count}"
            )
        return proxy, tuple(notes)
    finally:
        if evaluated_mesh is not None and temp_obj is not None:
            temp_obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
        if temp_obj is not None:
            bpy.data.objects.remove(temp_obj, do_unlink=True)
        if temp_mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(temp_mesh)


def _mesh_data_from_blender_mesh(mesh, *, triangulate: bool) -> MeshData:
    vertices = tuple(tuple(float(component) for component in vertex.co) for vertex in mesh.vertices)
    if triangulate:
        mesh.calc_loop_triangles()
        faces = tuple(tuple(int(index) for index in triangle.vertices) for triangle in mesh.loop_triangles)
    else:
        faces = tuple(tuple(int(index) for index in polygon.vertices) for polygon in mesh.polygons)
    hard_edges = frozenset(
        tuple(sorted(int(index) for index in edge.vertices))
        for edge in mesh.edges
        if edge.use_seam or getattr(edge, "use_edge_sharp", False)
    )
    return MeshData(vertices, faces, hard_edges)


def _mark_feature_edges(mesh, hard_edges: frozenset[tuple[int, int]]) -> None:
    edge_counts: dict[tuple[int, int], int] = defaultdict(int)
    for polygon in mesh.polygons:
        vertices = tuple(polygon.vertices)
        for edge in _face_edges(vertices):
            edge_counts[_edge_key(*edge)] += 1
    feature_edges = {_edge_key(*edge) for edge in hard_edges}
    feature_edges.update(edge for edge, count in edge_counts.items() if count == 1)
    for edge in mesh.edges:
        key = _edge_key(int(edge.vertices[0]), int(edge.vertices[1]))
        if key in feature_edges:
            edge.use_seam = True
            edge.use_edge_sharp = True


def _assign_protected_vertices(obj, mesh: MeshData) -> None:
    protected = _protected_vertex_indices(mesh)
    if not protected:
        return
    group = obj.vertex_groups.new(name="ZZ_LargeMeshProtected")
    group.add(sorted(protected), 1.0, "ADD")


def _protected_vertex_indices(mesh: MeshData) -> set[int]:
    protected = {vertex for edge in mesh.hard_edges for vertex in edge}
    edge_counts: dict[tuple[int, int], int] = defaultdict(int)
    for face in mesh.faces:
        for edge in _face_edges(face):
            edge_counts[_edge_key(*edge)] += 1
    for edge, count in edge_counts.items():
        if count == 1:
            protected.update(edge)
    return protected


class _OriginalSurface:
    def __init__(self, mesh: MeshData):
        self.mesh = mesh
        self.triangles = _triangles_with_source_faces(mesh)
        if not self.triangles:
            raise ValueError("원본 표면을 만들 삼각형이 없습니다.")
        self._bvh = None

    @property
    def bvh(self):
        if self._bvh is None:
            from mathutils import Vector
            from mathutils.bvhtree import BVHTree

            vertices = [Vector(vertex) for vertex in self.mesh.vertices]
            polygons = [triangle for triangle, _source in self.triangles]
            self._bvh = BVHTree.FromPolygons(vertices, polygons, all_triangles=True)
        return self._bvh

    def sample_density(
        self,
        vertices: Sequence[Vector3],
        density_values: Sequence[float],
        *,
        cancelled: CancelledCallback | None,
    ) -> tuple[float, ...]:
        if not density_values:
            return ()
        if len(density_values) != len(self.mesh.vertices):
            raise ValueError("원본 밀도 값 개수는 원본 메시 정점 개수와 같아야 합니다.")
        sampled = []
        for index, vertex in enumerate(vertices):
            if index % 256 == 0:
                _check_cancelled(cancelled)
            location, _normal, face_index, _distance = self.bvh.find_nearest(vertex)
            if location is None or face_index is None:
                raise ValueError("원본 표면에서 밀도를 샘플할 점을 찾지 못했습니다.")
            triangle, _source_face = self.triangles[int(face_index)]
            weights = _barycentric(tuple(float(component) for component in location), *(self.mesh.vertices[i] for i in triangle))
            density = sum(float(density_values[vertex_index]) * weight for vertex_index, weight in zip(triangle, weights))
            if not isfinite(density) or density < 0.0:
                raise ValueError("원본 표면에서 유효한 밀도 값을 샘플하지 못했습니다.")
            sampled.append(density)
        return tuple(sampled)

    def surface_error(
        self,
        mesh: MeshData,
        *,
        cancelled: CancelledCallback | None,
    ) -> tuple[float, float]:
        distances = []
        samples = list(mesh.vertices)
        samples.extend(_centroid(mesh.vertices[index] for index in face) for face in mesh.faces)
        for index, point in enumerate(samples):
            if index % 256 == 0:
                _check_cancelled(cancelled)
            _location, _normal, _face_index, distance = self.bvh.find_nearest(point)
            distances.append(float(distance or 0.0))
        return max(distances, default=0.0), sum(distances) / max(1, len(distances))


def _triangles_with_source_faces(mesh: MeshData) -> tuple[tuple[tuple[int, int, int], int], ...]:
    import bpy

    temp_mesh = bpy.data.meshes.new("ZZ_LargeMeshSourceTessellation")
    try:
        temp_mesh.from_pydata(mesh.vertices, [], mesh.faces)
        temp_mesh.update(calc_edges=True)
        temp_mesh.calc_loop_triangles()
        return tuple(
            (tuple(int(index) for index in triangle.vertices), int(triangle.polygon_index))
            for triangle in temp_mesh.loop_triangles
        )
    finally:
        if temp_mesh.name in bpy.data.meshes:
            bpy.data.meshes.remove(temp_mesh)


def _proxy_triangle_target(mesh: MeshData) -> int:
    triangle_count = sum(max(1, len(face) - 2) for face in mesh.faces)
    return max(256, min(MAX_PROXY_TRIANGLES, triangle_count))


def _verify_boundary_survival(source_analysis, output_analysis, label: str) -> None:
    if source_analysis.boundary_edge_count and output_analysis.boundary_edge_count == 0:
        raise ValueError(f"{label}에서 원본 경계가 모두 사라져 결과 적용을 중단했습니다.")


def _verify_explicit_features(source: MeshData, output: MeshData, label: str) -> None:
    if not source.hard_edges:
        return
    if not output.hard_edges:
        raise ValueError(f"{label}에서 명시 seam/hard edge가 모두 사라져 결과 적용을 중단했습니다.")
    tolerance = max(_bbox_scale(source.vertices) * MAX_SURFACE_ERROR_RATIO, 1.0e-8)
    missing = _count_missing_feature_vertices(source, output, tolerance)
    total = len({vertex for edge in source.hard_edges for vertex in edge})
    if total and missing / total > 0.1:
        raise ValueError(
            f"{label}에서 명시 seam/hard edge 정점 보존이 부족합니다: "
            f"missing={missing}, total={total}"
        )


def _count_missing_feature_vertices(source: MeshData, output: MeshData, tolerance: float) -> int:
    from mathutils import Vector
    from mathutils.kdtree import KDTree

    tree = KDTree(len(output.vertices))
    for index, vertex in enumerate(output.vertices):
        tree.insert(Vector(vertex), index)
    tree.balance()
    missing = 0
    for vertex_index in {vertex for edge in source.hard_edges for vertex in edge}:
        _co, _index, distance = tree.find(Vector(source.vertices[vertex_index]))
        if distance > tolerance:
            missing += 1
    return missing


def _face_edges(face: Sequence[int]):
    return tuple(zip(face, (*face[1:], face[0])))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first < second else (second, first)


def _bbox_scale(vertices: Sequence[Vector3]) -> float:
    if not vertices:
        return 0.0
    return max(max(vertex[axis] for vertex in vertices) - min(vertex[axis] for vertex in vertices) for axis in range(3))


def _barycentric(point: Vector3, a: Vector3, b: Vector3, c: Vector3) -> tuple[float, float, float]:
    v0 = _sub(b, a)
    v1 = _sub(c, a)
    v2 = _sub(point, a)
    d00 = _dot(v0, v0)
    d01 = _dot(v0, v1)
    d11 = _dot(v1, v1)
    d20 = _dot(v2, v0)
    d21 = _dot(v2, v1)
    denom = d00 * d11 - d01 * d01
    if abs(denom) <= 1.0e-30:
        return (1.0, 0.0, 0.0)
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1.0 - v - w
    return (u, v, w)


def _quad_aspect_stats(vertices: Sequence[Vector3], faces: Sequence[tuple[int, ...]]) -> tuple[float, float]:
    ratios = []
    for face in faces:
        points = [vertices[index] for index in face]
        lengths = [_distance(first, second) for first, second in zip(points, (*points[1:], points[0]))]
        shortest = min(lengths, default=0.0)
        longest = max(lengths, default=0.0)
        ratios.append(longest / shortest if shortest > 1.0e-12 else float("inf"))
    if not ratios:
        return float("inf"), float("inf")
    if any(not isfinite(ratio) for ratio in ratios):
        return float("inf"), float("inf")
    return max(ratios), sum(ratios) / len(ratios)


def _centroid(points) -> Vector3:
    total = [0.0, 0.0, 0.0]
    count = 0
    for point in points:
        count += 1
        total[0] += point[0]
        total[1] += point[1]
        total[2] += point[2]
    return (total[0] / count, total[1] / count, total[2] / count)


def _sub(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vector3, b: Vector3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _distance(a: Vector3, b: Vector3) -> float:
    return sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("작업이 취소되었습니다.")


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(max(0.0, min(1.0, fraction)), message)
