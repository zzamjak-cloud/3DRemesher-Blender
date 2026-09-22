"""복셀 리메시와 QuadriFlow 로 임의 형상에 새 쿼드 와이어를 까는 범용 경로.

격자 전략이 다루지 못하는 일반 캐릭터·소품을 위한 경로다. 순서는 복셀 리메시(촘촘히) → 파편 제거 →
데시메이트 → 매니폴드 수리 → (대칭이면 양의 반쪽만 절단) → QuadriFlow(자식 프로세스, 시드 사다리) →
작은 구멍 쿼드 메움 → 투영 슈링크랩과 릴랙스 → (대칭이면 미러 용접) → 원본 표면 기준 품질 검사다.
모디파이어는 depsgraph 평가로 적용해 오퍼레이터 컨텍스트에 의존하지 않고, QuadriFlow 만 bpy.ops 라 별도
Blender 프로세스에서 시간 제한을 두고 돌린다.

QuadriFlow 자체의 대칭 모드는 쓰지 않는다 — 실측(2026-09-21, Suzanne): 모든 시드·옵션에서 대칭면을 따라
84~115엣지짜리 구멍 두 개를 남겼다. 반쪽을 `use_preserve_boundary` 로 깔고 미러 모디파이어로 용접하면
구멍 없이 정확히 대칭인 결과가 나온다. 이 프로젝트의 "로컬 양의 축 기준 절단 후 미러" 규약과도 같다.

어느 단계가 실패해도 입력 `MeshData` 와 씬의 다른 오브젝트는 건드리지 않는다. 임시 오브젝트만 지운다.
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .core import (
    CancelledCallback,
    EngineInput,
    MeshData,
    ProgressCallback,
    RemeshCancelled,
    RemeshQuality,
    RemeshResult,
    Vector3,
    analyze_mesh,
)
from .engine import MAX_ACCEPTED_ASPECT_RATIO, MAX_OUTPUT_QUADS, MAX_SURFACE_ERROR_RATIO
from .ring_cut import BAND_LOOSE, RingCut, cut_bands, in_band, mirror_cuts, ring_cuts, stitch_bands
from .topology.quality import ring_propagation

MAX_SOURCE_FACES = 400000        # 이보다 큰 입력은 파이썬 변환 비용이 커 큰 메시 프록시 경로에 맡긴다
MAX_BOUNDARY_EDGE_RATIO = 0.2    # 경계 엣지가 이 비율을 넘는 열린 판은 부피가 없어 복셀 리메시가 베개 형상을 만든다
MIN_QUAD_RATIO = 0.95            # 구멍 메우기·삼각형 병합 뒤에도 남는 삼각형 허용 비율
MERGE_DIST = 2e-4                # QuadriFlow 사전 검사가 '길이 0'으로 보는 1e-4 미만 엣지를 이 길이까지 늘린다
FRAGMENT_RATIO = 0.005           # 전체 면수의 이 비율 미만인 떨어진 셸은 복셀 거품으로 보고 지운다
REPAIR_ROUNDS = 4
# (복셀 리메시 면수, 데시메이트 뒤 삼각형 수). 복셀은 촘촘히 굽어 좁은 틈을 살리고 데시메이트로 QuadriFlow 가
# 받는 크기까지 낮춘다. 데시메이트 비율이 2배를 넘으면 틈이 다시 메워지고, 삼각형 2만을 넘으면 QuadriFlow 가
# 정지·실패하므로 실패하면 다음 단으로 내려간다.
QF_INPUT_LADDER = ((32000, 16000), (32000, 11000), (20000, 8000))
QF_INPUT_FACES = 14000
VOXEL_SIZE_MIN_DIV = 400.0       # 복셀 한 변 하한 = 모델 크기 / 이 값
VOXEL_SIZE_MAX_DIV = 8.0         # 복셀 한 변 상한 = 모델 크기 / 이 값
QF_MIN_RATIO = 0.3               # QuadriFlow 결과 면수가 목표의 이 비율 밖이면 실패로 본다
QF_MAX_RATIO = 3.0
QF_EXTRA_SHELLS = 2              # 출력 셸 수가 입력보다 이만큼 넘게 많으면 조각난 출력
QF_MAX_BOUNDARY = 0.05           # 대칭면 밖의 구멍 엣지가 전체 엣지의 이 비율을 넘으면 깨진 출력 (정상 0~3%)
HOLE_MAX_EDGES = 16              # 이보다 큰 구멍은 메우지 않고 출력을 버린다 — 큰 구멍은 표면을 덮지 못한 것이다
QF_REQUEST_SCALE = 1.15          # 닫힌 입력의 QuadriFlow 결과는 요청의 0.67~0.96배로 모자라게 나온다
QF_HALF_REQUEST_SCALE = 1.0      # 경계 보존 반쪽 입력은 요청의 1.05~1.1배로 나온다
PLANE_TOLERANCE_RATIO = 1e-3     # QuadriFlow 출력의 경계 루프가 이 비율(모델 크기 기준) 안에 있으면 대칭면 루프로 본다
SNAP_TOLERANCE_RATIO = 1e-5      # 절단 직후 이 비율 안의 정점만 평면에 붙인다 — 넓게 붙이면 미세 엣지가 생겨 QuadriFlow 가 거절한다
QF_TIMEOUT = 25.0                # 시도 하나의 기본 제한 시간(초). 목표가 크면 늘린다
QF_TIMEOUT_PER_QUAD = 1.0 / 400  # 목표 쿼드 하나당 추가 시간(초)
QF_ATTEMPTS = 3                  # 한 밀도에서 시드를 바꿔 볼 횟수 — 정지는 비결정적이다
QF_TOTAL_BUDGET = 240.0          # 사다리 전체에 허용하는 시간(초). 넘으면 실패로 보고 다음 경로에 넘긴다
SHRINK_LIMIT = 3.0               # 노멀 투영 한계 = 복셀 한 변 x 이 배수
RELAX_ROUNDS = 2
RELAX_FACTOR = 0.5
SLIVER_ASPECT = 8.0              # 이 종횡비를 넘는 면의 정점만 골라 다시 편다 — 실측(갱스터 6,000쿼드): 22.2 → 7.6
SLIVER_ROUNDS = 20               # 슬리버 완화 반복 상한. 남는 몇 개는 형상이 실제로 접힌 곳이다
RING_CHECK_DEPTH = 4             # LOOP 접합부 바깥으로 닫힌 평행 링을 이 개수까지 세어 보고한다
BAND_LOOP_MAX_RATIO = 3.0        # 절단 띠 안의 경계 루프가 예상 링 둘레(엣지 수)의 이 배수를 넘으면 구멍으로 본다
BORDER_WELD_RATIO = 0.05         # 구멍·대칭면 경계 정점을 전형 엣지 길이의 이 비율 안에서 용접한다
TINY_EDGE_RATIO = 0.1            # 전형 엣지 길이의 이 비율보다 짧은 출력 엣지는 QuadriFlow 가 남긴 미세 면 뭉치다
TINY_EDGE_MAX_SHARE = 0.005      # 미세 엣지가 전체의 이 비율을 넘는 출력은 버리고 다음 시드로 간다 (실측 2026-09-22, Y 대칭+절단 링: 붕괴시키면 삼각형 44개)

_WORKER = Path(__file__).with_name("quadriflow_worker.py")
_TEMP_ROOT: Path | None = None


def set_temp_root(directory: Path | str | None) -> None:
    """QuadriFlow 중간 .blend 를 둘 폴더. worker 는 부모가 정리하는 작업 폴더를 지정한다."""
    global _TEMP_ROOT
    _TEMP_ROOT = Path(directory) if directory is not None else None


def is_available() -> bool:
    """bpy·bmesh·mathutils 를 쓸 수 있는 Blender Python 환경인지."""
    try:
        import bpy  # noqa: F401
        import bmesh  # noqa: F401
        import mathutils  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def unsupported_reason(engine_input: EngineInput) -> str:
    """이 경로가 입력을 받을 수 없으면 사유를, 받을 수 있으면 빈 문자열을 돌려준다."""
    strips = tuple(guide.name for guide in engine_input.guide_curves if "STRIP" in guide.kind)
    if strips:
        return f"QuadriFlow 경로는 필수 STRIP 가이드를 출력 엣지로 보존하지 못합니다: {', '.join(strips)}"
    if engine_input.settings.target_quad_count > MAX_OUTPUT_QUADS:
        return f"목표 쿼드 수가 출력 상한 {MAX_OUTPUT_QUADS}을 넘습니다."
    if len(engine_input.mesh.faces) > MAX_SOURCE_FACES:
        return f"입력 면 수가 QuadriFlow 경로 상한 {MAX_SOURCE_FACES}을 넘습니다: {len(engine_input.mesh.faces)}"
    analysis = engine_input.analysis
    if analysis.edge_count and analysis.boundary_edge_count / analysis.edge_count > MAX_BOUNDARY_EDGE_RATIO:
        return (
            f"경계 엣지 비율이 {analysis.boundary_edge_count / analysis.edge_count:.0%} 인 열린 판 형상은 "
            "복셀 리메시로 부피를 만들 수 없어 QuadriFlow 경로를 건너뜁니다."
        )
    return ""


def unsupported_controls(engine_input: EngineInput) -> tuple[str, ...]:
    """QuadriFlow 경로가 반영하지 못하는 입력 제어. AUTO 는 이 제어가 있으면 실험 엔진을 우선한다."""
    controls = []
    if engine_input.density_values and len(set(round(v, 6) for v in engine_input.density_values)) > 1:
        controls.append("밀도 속성")
    if any("DIRECTION" in guide.kind for guide in engine_input.guide_curves):
        controls.append("DIRECTION 가이드")
    return tuple(controls)


def remesh_quadriflow(
    engine_input: EngineInput,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelledCallback | None = None,
) -> RemeshResult:
    """입력 메시를 복셀 리메시·QuadriFlow 로 다시 깔고 원본 표면에 붙인 결과를 돌려준다."""
    if not is_available():
        raise RuntimeError("QuadriFlow 경로는 Blender Python 환경에서 실행해야 합니다.")
    reason = unsupported_reason(engine_input)
    if reason:
        raise ValueError(reason)

    source = engine_input.mesh
    source.validate()
    settings = engine_input.settings
    target = settings.target_quad_count
    scale = _bbox_scale(source.vertices)
    if scale <= 0.0:
        raise ValueError("입력 메시의 크기가 0입니다.")
    axes = tuple(sorted(set(settings.symmetry_axes)))
    started = time.monotonic()

    _check_cancelled(cancelled)
    _report(progress, 0.02, "QuadriFlow 경로 준비")
    source_obj = None
    work_obj = None
    try:
        source_obj = _make_object("ZZ_QuadriFlowSource", source)
        work_obj = _make_object("ZZ_QuadriFlowWork", source)
        warnings: list[str] = []
        unsupported = list(unsupported_controls(engine_input))
        if "밀도 속성" in unsupported:
            warnings.append("QuadriFlow 경로는 밀도 속성을 반영하지 않고 균일 밀도로 배치했습니다.")
        if "DIRECTION 가이드" in unsupported:
            warnings.append("QuadriFlow 경로는 DIRECTION 가이드를 반영하지 않았습니다.")
        if source.hard_edges:
            warnings.append("복셀 리메시가 원본 연결을 새로 만들므로 명시 특징선은 위치 기준으로만 근사됩니다.")

        source_area = sum(polygon.area for polygon in source_obj.data.polygons)
        voxel = _voxel_size(source_area, scale, QF_INPUT_LADDER[0][0])
        # LOOP 가이드는 절단 링이 된다. 대칭이면 양의 반쪽만 깔리므로 음의 쪽 루프는 양의 쪽으로 미러해 처리한다.
        requested_cuts = mirror_cuts(ring_cuts(engine_input, math.sqrt(source_area / max(target, 1))), axes)
        cuts: tuple[RingCut, ...] = ()
        half_output = False
        success = False
        failure_notes: list[str] = []
        ladder = list(QF_INPUT_LADDER)
        step = -1
        seam_reports: tuple = ()
        stitch_retries = 0
        while step + 1 < len(ladder):
            step += 1
            density, triangles = ladder[step]
            cuts = ()  # 단계마다 복셀 리메시로 새 메시가 되므로 이전 단계의 절단 링을 이어 쓰지 않는다
            _check_cancelled(cancelled)
            remaining = QF_TOTAL_BUDGET - (time.monotonic() - started)
            if remaining <= 0.0:
                failure_notes.append(f"전체 시간 예산 {QF_TOTAL_BUDGET:.0f}초를 넘겨 남은 단계를 건너뛰었습니다.")
                break
            base = 0.05 + step * 0.22
            _report(progress, base, f"복셀 리메시 (목표 {density}면)")
            voxel = _voxel_remesh(work_obj, source_obj.data, source_area, scale, density)
            if len(work_obj.data.polygons) == 0:
                failure_notes.append(f"{density}면 복셀 리메시 결과가 비었습니다.")
                continue
            removed = _remove_fragments(work_obj)
            if removed:
                warnings.append(f"복셀 리메시가 남긴 작은 조각 {removed}개를 지웠습니다.")
            _check_cancelled(cancelled)
            _report(progress, base + 0.06, f"삼각형 {triangles}개로 데시메이트")
            _decimate(work_obj, triangles)
            _report(progress, base + 0.08, "매니폴드 수리")
            if not _make_manifold(work_obj):
                failure_notes.append(f"{density}/{triangles} 단계에서 QuadriFlow 입력 조건을 만들지 못했습니다.")
                continue
            if requested_cuts:
                _report(progress, base + 0.09, f"LOOP 가이드 {len(requested_cuts)}개 위치에 절단 띠 생성")
                cuts, skipped = cut_bands(work_obj.data, requested_cuts, scale, min(MERGE_DIST, 1e-3 * scale), cancelled=cancelled)
                # 쓸 수 없는 루프는 결과를 막지 않고 경고로 알려 위치를 고칠 수 있게 한다
                warnings.extend(skipped)
                remaining = QF_TOTAL_BUDGET - (time.monotonic() - started)
                if remaining <= 0.0:
                    failure_notes.append(f"전체 시간 예산 {QF_TOTAL_BUDGET:.0f}초를 넘겨 남은 단계를 건너뛰었습니다.")
                    break
            _check_cancelled(cancelled)
            _report(progress, base + 0.10, f"QuadriFlow 실행 (목표 {target}쿼드)")
            outcome = _quadriflow(work_obj, target, axes, scale, remaining, cuts, cancelled=cancelled)
            if outcome is None:
                failure_notes.append(f"{density}/{triangles} 단계의 QuadriFlow 시도가 모두 실패했습니다.")
                continue
            if cuts:
                _check_cancelled(cancelled)
                _report(progress, base + 0.18, "절단 링 접합")
                seam_reports = stitch_bands(work_obj.data, cuts, cancelled=cancelled)
                failed = [
                    seam for seam in seam_reports
                    if not seam.bridged or (not seam.closed and not (axes if outcome else ()))
                ]
                if failed and stitch_retries < len(requested_cuts):
                    # 접합 못 한 루프는 빼고 같은 단계를 다시 돈다 — 나머지 루프의 링은 살린다
                    for seam in failed:
                        warnings.append(
                            f"LOOP '{seam.name}' 의 절단 링을 접합하지 못해({seam.note or '링이 닫히지 않음'}, "
                            f"양쪽 {seam.ring_sizes[0]}·{seam.ring_sizes[1]}정점) 이 루프를 빼고 다시 깔았습니다. "
                            "단면이 일정한 위치로 옮겨 주세요."
                        )
                    names = {seam.name for seam in failed}
                    requested_cuts = tuple(cut for cut in requested_cuts if cut.name not in names)
                    stitch_retries += 1
                    step -= 1
                    continue
                if failed:
                    raise ValueError(
                        f"LOOP 가이드 '{failed[0].name}' 의 절단 링을 접합하지 못했습니다 "
                        f"(양쪽 링 정점 {failed[0].ring_sizes[0]}·{failed[0].ring_sizes[1]}, {failed[0].note or '링이 닫히지 않음'})."
                    )
            half_output = outcome
            success = True
            break
        if not success:
            raise ValueError("QuadriFlow 가 쓸 수 있는 쿼드 메시를 만들지 못했습니다. " + " ".join(failure_notes))
        plane_axes = axes if half_output else ()
        for seam in seam_reports:
            warnings.append(
                f"LOOP '{seam.name}': 절단 링 {seam.ring_sizes[0]}·{seam.ring_sizes[1]}정점을 접합했습니다"
                + (f" (전이 삼각형 {seam.triangles}개)." if seam.triangles else ".")
            )

        _check_cancelled(cancelled)
        _report(progress, 0.74, "QuadriFlow 출력 정리")
        _repair_output(work_obj, plane_axes, scale)  # QuadriFlow 는 표면 곳곳에 작은 구멍을 남긴다

        _check_cancelled(cancelled)
        _report(progress, 0.80, "원본 표면에 투영")
        _shrinkwrap(work_obj, source_obj, voxel * SHRINK_LIMIT, plane_axes, scale, cancelled=cancelled)
        if plane_axes:
            _report(progress, 0.88, "대칭면 기준 미러 용접")
            _mirror(work_obj, plane_axes, scale)

        _check_cancelled(cancelled)
        _report(progress, 0.90, "원본 표면 기준 품질 검사")
        output = _mesh_data_from_object(work_obj)
        output.validate()
        if len(output.faces) > MAX_OUTPUT_QUADS:
            raise ValueError(f"QuadriFlow 결과가 출력 상한 {MAX_OUTPUT_QUADS}쿼드를 초과했습니다.")
        analysis = analyze_mesh(output)
        if analysis.non_manifold_edge_count or analysis.degenerate_face_count:
            raise ValueError(
                f"QuadriFlow 결과의 위상 검증에 실패했습니다: 비다양체 엣지 {analysis.non_manifold_edge_count}, "
                f"퇴화 면 {analysis.degenerate_face_count}"
            )
        if analysis.quad_ratio < MIN_QUAD_RATIO:
            raise ValueError(f"QuadriFlow 결과의 쿼드 비율이 낮습니다: {analysis.quad_ratio:.1%} < {MIN_QUAD_RATIO:.0%}")
        if analysis.quad_ratio < 1.0:
            warnings.append(f"구멍 메우기로 남은 삼각형 {analysis.triangle_count}개가 있습니다 (쿼드 비율 {analysis.quad_ratio:.1%}).")
        if analysis.n_gon_count:
            raise ValueError(f"QuadriFlow 결과에 N각형 {analysis.n_gon_count}개가 남았습니다.")

        max_out, mean_out, max_src, mean_src = _bidirectional_distance(source, output, cancelled=cancelled)
        if max_out > MAX_SURFACE_ERROR_RATIO * scale:
            raise ValueError(f"QuadriFlow 결과가 원본 표면에서 너무 멉니다: {max_out:.6g} > {MAX_SURFACE_ERROR_RATIO * scale:.6g}")
        if max_src > MAX_SURFACE_ERROR_RATIO * scale:
            # 복셀 리메시는 좁은 틈과 안쪽 껍질을 닫으므로 원본 쪽 표본은 멀어질 수 있다. 결과 표면은 원본 위에 있다.
            warnings.append(
                f"원본의 일부 표본이 결과 표면에서 {max_src:.4g} 떨어져 있습니다. 복셀 리메시가 좁은 틈이나 내부 면을 닫았을 수 있습니다."
            )
        max_aspect, mean_aspect = _quad_aspect_stats(output.vertices, output.faces)
        if max_aspect > MAX_ACCEPTED_ASPECT_RATIO:
            raise ValueError(
                f"QuadriFlow 결과에 지나치게 길고 좁은 면이 있습니다: 최대 종횡비 {max_aspect:.2f} > {MAX_ACCEPTED_ASPECT_RATIO:.0f}"
            )
        symmetry_error = _symmetry_error(output.vertices, axes) if axes else 0.0
        if axes and symmetry_error > 1.0e-6 * scale:
            raise ValueError(f"QuadriFlow 결과가 요청한 대칭을 지키지 못했습니다: 대칭 오차 {symmetry_error:.4g}")
        target_error = abs(analysis.quad_count - target) / max(1, target)
        if analysis.quad_count != target:
            warnings.append(f"QuadriFlow 배치로 목표와 실제 개수가 다릅니다: target={target}, actual={analysis.quad_count} ({target_error:.1%})")
        if analysis.boundary_edge_count:
            # 복셀 리메시가 표면을 닫으므로 남은 경계는 메우지 못한 구멍이거나 미러 용접 실패다
            raise ValueError(f"QuadriFlow 결과에 메우지 못한 열린 경계 {analysis.boundary_edge_count}개가 남았습니다.")
        if engine_input.analysis.boundary_edge_count:
            warnings.append("복셀 리메시가 열린 경계를 닫아 결과는 닫힌 표면입니다.")
        for cut in cuts:
            warnings.append(_ring_report(output, cut))
        warnings.insert(0, f"QuadriFlow 경로: 복셀 리메시 뒤 새 와이어를 깔고 원본 표면에 투영했습니다 ({time.monotonic() - started:.1f}초).")

        _report(progress, 1.0, "QuadriFlow 완료")
        return RemeshResult(
            output,
            RemeshQuality(
                target_quad_count=target,
                actual_quad_count=analysis.quad_count,
                quad_ratio=analysis.quad_ratio,
                boundary_edge_count=analysis.boundary_edge_count,
                non_manifold_edge_count=analysis.non_manifold_edge_count,
                degenerate_face_count=analysis.degenerate_face_count,
                max_aspect_ratio=max_aspect,
                mean_aspect_ratio=mean_aspect,
                target_error_ratio=target_error,
                max_surface_error=max(max_out, max_src),
                mean_surface_error=(mean_out + mean_src) / 2.0,
                symmetry_error=symmetry_error,
                field_alignment=0.0,
            ),
            tuple(dict.fromkeys(warnings)),
            tuple(unsupported),
        )
    finally:
        _remove_object(work_obj)
        _remove_object(source_obj)


# --- 임시 오브젝트 ---------------------------------------------------------

def _make_object(name: str, mesh: MeshData):
    import bpy

    data = bpy.data.meshes.new(name)
    obj = None
    try:
        data.from_pydata([tuple(v) for v in mesh.vertices], [], [tuple(f) for f in mesh.faces])
        data.update(calc_edges=True)
        data.validate(clean_customdata=False)
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.update()
    except Exception:
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.meshes.remove(data)
        raise
    return obj


def _remove_object(obj) -> None:
    import bpy

    if obj is None:
        return
    try:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except ReferenceError:
        pass


def _apply_modifiers(obj) -> None:
    """모디파이어 스택을 depsgraph 로 평가해 메시에 굽는다. 오퍼레이터 컨텍스트가 필요 없다."""
    import bpy

    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    baked = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=False, depsgraph=depsgraph)
    obj.modifiers.clear()
    stale = obj.data
    name = stale.name
    obj.data = baked
    if stale.users == 0:
        bpy.data.meshes.remove(stale)
    baked.name = name
    bpy.context.view_layer.update()


def _replace_mesh(obj, mesh) -> None:
    import bpy

    stale = obj.data
    obj.data = mesh
    if stale.users == 0:
        bpy.data.meshes.remove(stale)
    bpy.context.view_layer.update()


# --- 복셀 리메시·데시메이트 ---------------------------------------------------

def _voxel_size(area: float, size: float, wanted: float) -> float:
    """복셀 리메시 결과가 wanted 면수 근처가 되도록 표면적에서 역산한 복셀 한 변."""
    voxel = math.sqrt(max(area, 1e-12) / max(wanted, 1.0))
    return min(max(voxel, size / VOXEL_SIZE_MIN_DIV), size / VOXEL_SIZE_MAX_DIV)


def _voxel_remesh(obj, source_mesh, area: float, size: float, wanted: int) -> float:
    """원본 사본을 복셀 리메시해 열린 셸·겹친 면을 하나의 닫힌 표면으로 녹이고 실제 쓴 복셀 한 변을 돌려준다.

    면수는 복셀 한 변으로 정확히 예측되지 않아 재어 보고 보정한다. 리메시 결과를 다시 리메시하면 표면이
    뭉개지므로 매번 원본 사본에서 시작한다."""
    voxel = _voxel_size(area, size, wanted)
    for _ in range(3):
        _replace_mesh(obj, source_mesh.copy())
        modifier = obj.modifiers.new("ZZ_Voxel", "REMESH")
        modifier.mode = "VOXEL"
        modifier.voxel_size = voxel
        modifier.adaptivity = 0.0
        modifier.use_remove_disconnected = False
        _apply_modifiers(obj)
        faces = len(obj.data.polygons)
        if faces == 0 or wanted * 0.6 <= faces <= wanted * 1.4:
            break
        # 면수는 복셀 한 변의 제곱에 반비례한다 — 그 비율로 보정한다
        voxel = min(max(voxel * math.sqrt(faces / wanted), size / VOXEL_SIZE_MIN_DIV), size / VOXEL_SIZE_MAX_DIV)
    return voxel


def _decimate(obj, target_triangles: int) -> None:
    """Decimate(COLLAPSE)로 목표 삼각형 수까지 줄인다. 결과는 전부 삼각형이다."""
    obj.data.calc_loop_triangles()
    triangles = len(obj.data.loop_triangles)
    modifier = obj.modifiers.new("ZZ_Decimate", "DECIMATE")
    modifier.decimate_type = "COLLAPSE"
    modifier.ratio = min(1.0, float(target_triangles) / float(max(1, triangles)))
    if hasattr(modifier, "use_collapse_triangulate"):
        modifier.use_collapse_triangulate = True
    _apply_modifiers(obj)


def _remove_fragments(obj) -> int:
    """전체 면수의 FRAGMENT_RATIO 미만인 떨어진 셸을 지운다. 촘촘한 복셀 리메시는 좁은 틈에 거품 셸을 남긴다."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    shells = _shells(bm)
    if len(shells) <= 1:
        bm.free()
        return 0
    threshold = max(int(len(bm.faces) * FRAGMENT_RATIO), 1)
    small = [shell for shell in shells if len(shell) < threshold]
    if not small or len(small) == len(shells):
        bm.free()
        return 0
    bmesh.ops.delete(bm, geom=[face for shell in small for face in shell], context="FACES")
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return len(small)


def _shells(bm) -> list[list]:
    seen: set = set()
    shells = []
    for face in bm.faces:
        if face in seen:
            continue
        stack, shell = [face], []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            shell.append(current)
            for edge in current.edges:
                stack.extend(f for f in edge.link_faces if f not in seen)
        shells.append(shell)
    return shells


# --- 매니폴드 수리 ---------------------------------------------------------

def _quadriflow_ready(mesh) -> dict:
    """QuadriFlow 사전 검사와 같은 기준으로 메시 상태를 센다. 모두 0 이어야 통과한다.

    Blender 는 면이 2개가 아닌 엣지, 이웃 면의 winding 불일치, 길이 1e-4 미만 엣지를 모두 거절 사유로 본다."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    report = {
        "tiny": sum(1 for e in bm.edges if all(abs(e.verts[0].co[i] - e.verts[1].co[i]) < 1e-4 for i in range(3))),
        "open": sum(1 for e in bm.edges if len(e.link_faces) == 1),
        "edge": sum(1 for e in bm.edges if not e.is_manifold),
        "vert": sum(1 for v in bm.verts if not v.is_manifold),
        "wind": sum(1 for e in bm.edges if not e.is_contiguous),
    }
    bm.free()
    return report


def _make_manifold(obj, rounds: int = REPAIR_ROUNDS) -> bool:
    """QuadriFlow 가 받아들이는 상태로 만든다. 성공 여부를 돌려준다.

    미세 엣지는 녹이지 않고 정점을 밀어 늘리고(dissolve 는 나비 매듭 정점을 만들어 수렴하지 않는다),
    엣지에 셋 이상 붙은 면은 초과분만 지우고, 그때 생긴 구멍을 메우고, 뜬 정점을 걷어낸 뒤 노멀을 맞춘다."""
    import bmesh

    for _ in range(max(rounds, 1)):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        _stretch_tiny_edges(bm)
        extra = []
        for edge in bm.edges:
            if len(edge.link_faces) > 2:
                keep = sorted(edge.link_faces, key=lambda f: -f.calc_area())[:2]
                extra += [f for f in edge.link_faces if f not in keep]
        if extra:
            bmesh.ops.delete(bm, geom=list(set(extra)), context="FACES")
        border = [e for e in bm.edges if len(e.link_faces) == 1]
        if border:
            bmesh.ops.holes_fill(bm, edges=border, sides=64)
            border = [e for e in bm.edges if len(e.link_faces) == 1]
            if border:
                bmesh.ops.triangle_fill(bm, edges=border, use_beauty=True, use_dissolve=False)
        loose = [v for v in bm.verts if not v.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        if not any(_quadriflow_ready(obj.data).values()):
            return True
    return False


def _stretch_tiny_edges(bm) -> int:
    from mathutils import Vector

    moved = 0
    for edge in bm.edges:
        a, b = edge.verts
        if not all(abs(a.co[i] - b.co[i]) < 1e-4 for i in range(3)):
            continue
        direction = b.co - a.co
        if direction.length_squared < 1e-16:
            direction = b.normal if b.normal.length_squared > 0 else Vector((0.0, 0.0, 1.0))
        direction.normalize()
        b.co = a.co + direction * MERGE_DIST
        moved += 1
    return moved


# --- QuadriFlow ------------------------------------------------------------

def _quadriflow(
    obj, target: int, axes: tuple[str, ...], scale: float, budget: float, cuts: tuple[RingCut, ...] = (), *,
    cancelled: CancelledCallback | None,
) -> bool | None:
    """자식 Blender 에서 QuadriFlow 를 돌려 결과 메시로 바꾼다.

    대칭 축이 있으면 양의 반쪽만 잘라 경계를 보존한 채 깔고 True 를 돌려준다(호출자가 미러한다).
    비대칭 결과는 대칭 검사를 통과할 수 없으므로 반쪽이 실패하면 폴백 없이 None 을 돌려준다.
    대칭 축이 없으면 전체를 깔고 False, 실패하면 None 이다. 절단 띠가 있으면 그 경계도 보존한다."""
    import bpy

    max_shells = _shell_count(obj.data) + QF_EXTRA_SHELLS
    # 시도 하나의 제한은 남은 예산을 넘지 않게 자른다
    timeout = max(1.0, min(QF_TIMEOUT + target * QF_TIMEOUT_PER_QUAD, budget))
    temp_root = _TEMP_ROOT if _TEMP_ROOT is not None and _TEMP_ROOT.is_dir() else None
    with tempfile.TemporaryDirectory(prefix="zzamjak_qf_", dir=temp_root) as work_dir:
        if axes:
            half = _clip_positive(obj.data, axes, scale)
            if half is None:
                raise ValueError("대칭 축의 양의 영역에 면이 없어 반쪽을 만들 수 없습니다.")
            try:
                request = max(int(target / (2 ** len(axes)) * QF_HALF_REQUEST_SCALE), 4)
                src = os.path.join(work_dir, "half.blend")
                bpy.data.libraries.write(src, {half}, fake_user=True)
                mesh = _race_quadriflow(
                    src, work_dir, request, expected=request, preserve_boundary=True, max_shells=max_shells,
                    plane_axes=axes, scale=scale, timeout=timeout, attempts=QF_ATTEMPTS, cuts=cuts, cancelled=cancelled,
                )
            finally:
                bpy.data.meshes.remove(half)
            if mesh is None:
                return None
            mesh.use_fake_user = False
            _replace_mesh(obj, mesh)
            return True
        # 절단 띠가 있어도 닫힌 전체 입력은 요청보다 모자라게 나온다 (실측 2026-09-22, 복셀 입력: 요청 1,500 → 1,261)
        request = max(int(target * QF_REQUEST_SCALE), 4)
        src = os.path.join(work_dir, "in.blend")
        bpy.data.libraries.write(src, {obj.data}, fake_user=True)
        mesh = _race_quadriflow(
            src, work_dir, request, expected=target, preserve_boundary=bool(cuts), max_shells=max_shells,
            plane_axes=(), scale=scale, timeout=timeout, attempts=QF_ATTEMPTS, cuts=cuts, cancelled=cancelled,
        )
        if mesh is None:
            return None
        mesh.use_fake_user = False
        _replace_mesh(obj, mesh)
        return False


def _clip_positive(mesh, axes: tuple[str, ...], scale: float):
    """대칭 축마다 양의 반쪽만 남긴 새 메시. 절단면 정점은 정확히 평면에 놓는다. 남는 면이 없으면 None."""
    import bmesh
    import bpy

    tolerance = SNAP_TOLERANCE_RATIO * scale
    bm = bmesh.new()
    bm.from_mesh(mesh)
    for axis in axes:
        component = "XYZ".index(axis)
        normal = [0.0, 0.0, 0.0]
        normal[component] = 1.0
        bmesh.ops.bisect_plane(
            bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], plane_co=(0.0, 0.0, 0.0), plane_no=tuple(normal),
            clear_inner=True, clear_outer=False, dist=1e-6,
        )
    # 절단이 남긴 미세 엣지는 QuadriFlow 사전 검사에 걸리므로 병합한다 (실측: 17개 때문에 시드 전패).
    # 아주 작은 모델에서 형상을 무너뜨리지 않도록 모델 크기 기준으로도 자른다.
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=min(MERGE_DIST, 1e-3 * scale))
    for vertex in bm.verts:
        for axis in axes:
            component = "XYZ".index(axis)
            if abs(vertex.co[component]) < tolerance:
                vertex.co[component] = 0.0
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    if not bm.faces:
        bm.free()
        return None
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    half = bpy.data.meshes.new(mesh.name + "_half")
    bm.to_mesh(half)
    bm.free()
    half.update()
    return half


def _race_quadriflow(
    src: str,
    work_dir: str,
    request: int,
    *,
    expected: int,
    preserve_boundary: bool,
    max_shells: int,
    plane_axes: tuple[str, ...],
    scale: float,
    timeout: float,
    attempts: int,
    cuts: tuple[RingCut, ...] = (),
    cancelled: CancelledCallback | None,
):
    """시드를 바꿔 차례로 돌리고, 시간을 넘긴 시도는 죽이고 다음 시드로 넘어간다. 동시 실행은 서로 코어를 뺏어 더 느리다."""
    import bpy

    tag = "h" if preserve_boundary else "c"
    started = time.monotonic()
    for seed in range(1, attempts + 1):
        _check_cancelled(cancelled)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 1.0:
            break
        dst = os.path.join(work_dir, f"out{tag}{seed}.blend")
        command = [
            bpy.app.binary_path, "--background", "--factory-startup", "--python", str(_WORKER), "--",
            src, dst, str(request), "1" if preserve_boundary else "0", str(seed),
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None
        deadline = time.monotonic() + remaining
        returncode = None
        try:
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                if cancelled is not None and cancelled():
                    raise RemeshCancelled("작업이 취소되었습니다.")
                if time.monotonic() > deadline:
                    break
                time.sleep(0.1)
        finally:
            # 취소·시간 초과·SIGTERM 어느 경로로 빠져도 자식 QuadriFlow 를 남기지 않는다
            if process.poll() is None:
                _kill(process)
        if returncode != 0 or not os.path.isfile(dst):
            continue
        with bpy.data.libraries.load(dst) as (data_from, data_to):
            data_to.meshes = data_from.meshes[:1]
        if not data_to.meshes or data_to.meshes[0] is None:
            continue
        mesh = data_to.meshes[0]
        if _qf_output_ok(mesh, expected, max_shells, plane_axes, scale, cuts):
            return mesh
        bpy.data.meshes.remove(mesh)
    return None


def _kill(process) -> None:
    process.kill()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _qf_output_ok(
    mesh, expected: int, max_shells: int, plane_axes: tuple[str, ...], scale: float, cuts: tuple[RingCut, ...] = ()
) -> bool:
    """NaN 정점·큰 구멍·조각남·면수 이탈을 거른다. QuadriFlow 는 종료 코드 0 으로도 이런 메시를 낸다.
    대칭면 루프와 절단 띠 경계(캡이 없을 때)는 구멍이 아니다."""
    import bmesh

    faces = len(mesh.polygons)
    if not (expected * QF_MIN_RATIO <= faces <= expected * QF_MAX_RATIO):
        return False
    if any(not all(math.isfinite(c) for c in v.co) for v in mesh.vertices):
        return False
    if any(len(polygon.vertices) != 4 for polygon in mesh.polygons):
        return False
    bm = bmesh.new()
    bm.from_mesh(mesh)
    ok = True
    # 대칭면 경계 옆의 미세 면 뭉치(엣지가 전형의 1~20%)는 스무딩·메우기로 펴지지 않아 종횡비 검사에서 떨어진다
    lengths = sorted(edge.calc_length() for edge in bm.edges)
    if lengths:
        median = lengths[len(lengths) // 2]
        tiny = sum(1 for length in lengths if length < median * TINY_EDGE_RATIO)
        if tiny > len(lengths) * TINY_EDGE_MAX_SHARE:
            ok = False
    hole_edges = 0
    for loop in _boundary_loops(bm) if ok else ():
        if _loop_on_plane(loop, plane_axes, scale) or _loop_in_bands(loop, plane_axes, scale, cuts):
            continue
        if len(loop) > HOLE_MAX_EDGES:
            ok = False
            break
        hole_edges += len(loop)
    if ok and hole_edges > len(bm.edges) * QF_MAX_BOUNDARY:
        ok = False
    if ok and len(_shells(bm)) > max_shells:
        ok = False
    bm.free()
    return ok


def _shell_count(mesh) -> int:
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    count = len(_shells(bm))
    bm.free()
    return count


def _boundary_loops(bm) -> list[list]:
    """경계 엣지를 연결된 루프 단위로 묶는다."""
    border_list = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    border = set(border_list)
    seen: set = set()
    loops = []
    for edge in border_list:
        if edge in seen:
            continue
        loop = [edge]
        seen.add(edge)
        stack = [edge]
        while stack:
            current = stack.pop()
            for vertex in current.verts:
                for other in vertex.link_edges:
                    if other in border and other not in seen:
                        seen.add(other)
                        loop.append(other)
                        stack.append(other)
        loops.append(loop)
    return loops


def _plane_snap_tolerance(loop, scale: float) -> float:
    """대칭면 루프 정점을 평면으로 되돌릴 때 허용하는 거리.

    QuadriFlow 의 경계 보존은 근사라 밀도가 낮으면 경계 정점이 엣지 길이의 절반 가까이 평면에서 떠 있다
    (실측 2026-09-21, 옥탄트 128면: 0.049). 루프 엣지 길이 기준으로 허용치를 잡고 그 안은 평면에 붙인다."""
    lengths = sorted(edge.calc_length() for edge in loop)
    median = lengths[len(lengths) // 2] if lengths else 0.0
    return max(PLANE_TOLERANCE_RATIO * scale, 1.5 * median)


def _loop_on_plane(loop, plane_axes: tuple[str, ...], scale: float) -> bool:
    """경계 루프가 대칭 절단면 루프인지 — 정점 대부분이 평면 위에 있고 나머지도 엣지 길이 안에서 떠 있어야 한다."""
    if not plane_axes:
        return False
    tight = PLANE_TOLERANCE_RATIO * scale
    loose = _plane_snap_tolerance(loop, scale)
    vertices = {vertex for edge in loop for vertex in edge.verts}
    distances = [min(abs(vertex.co["XYZ".index(axis)]) for axis in plane_axes) for vertex in vertices]
    if any(distance >= loose for distance in distances):
        return False
    return sum(1 for distance in distances if distance < tight) >= 0.5 * len(distances)


def _loop_in_bands(loop, plane_axes: tuple[str, ...], scale: float, cuts: tuple[RingCut, ...]) -> bool:
    """경계 루프의 정점이 모두 절단 띠 안(또는 대칭면 위)에 있으면 절단이 만든 경계다.
    띠 안이라도 예상 링 둘레의 몇 배를 넘는 루프는 경계 주변을 덮지 못한 출력이다."""
    if not cuts:
        return False
    longest_ring = max(2.0 * math.pi * cut.radius / max(cut.half_width * 2.0, 1e-12) for cut in cuts)
    if len(loop) > longest_ring * BAND_LOOP_MAX_RATIO:
        return False
    loose = _plane_snap_tolerance(loop, scale) if plane_axes else 0.0
    for edge in loop:
        for vertex in edge.verts:
            if in_band(tuple(vertex.co), cuts):
                continue
            if plane_axes and min(abs(vertex.co["XYZ".index(axis)]) for axis in plane_axes) < loose:
                continue
            return False
    return True


def _ring_report(output: MeshData, cut: RingCut) -> str:
    """접합부 양쪽 바깥으로 닫힌 평행 링이 몇 개 이어지는지 — 나선이면 첫 링부터 닫히지 않는다.
    방향 라벨은 진행 방향이 가장 많이 향하는 모델 축(+X/−Y 등)으로 적는다."""
    offset = cut.half_width * BAND_LOOSE
    parts = []
    for sign in (-1.0, 1.0):
        center = tuple(cut.center[i] + cut.normal[i] * offset * sign for i in range(3))
        normal = tuple(cut.normal[i] * sign for i in range(3))
        dominant = max(range(3), key=lambda i: abs(normal[i]))
        label = ("+" if normal[dominant] >= 0.0 else "−") + "XYZ"[dominant]
        result = ring_propagation(output, center, normal, cut.radius, RING_CHECK_DEPTH)
        if not result.belt_ok:
            parts.append(f"{label}쪽 링 없음({result.message})")
        else:
            parts.append(f"{label}쪽 {result.ring_size}정점 링 뒤로 닫힌 링 {result.closed_rings}/{RING_CHECK_DEPTH}")
    return f"LOOP '{cut.name}' 링 전파: " + ", ".join(parts) + "."


def _ordered_loop_vertices(loop) -> list | None:
    """루프가 단순 폐곡선(정점마다 루프 엣지 2개)이면 순서대로 정점을 돌려준다."""
    adjacency: dict = {}
    for edge in loop:
        a, b = edge.verts
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    if any(len(neighbours) != 2 for neighbours in adjacency.values()):
        return None
    start = next(iter(adjacency))
    ordered = [start]
    previous, current = None, start
    while True:
        first, second = adjacency[current]
        following = second if first is previous else first
        if following is start:
            break
        ordered.append(following)
        previous, current = current, following
        if len(ordered) > len(loop):
            return None
    return ordered if len(ordered) == len(loop) else None


def _repair_output(obj, plane_axes: tuple[str, ...], scale: float) -> None:
    """QuadriFlow 가 남긴 작은 구멍을 쿼드로 메우고 대칭면 정점을 평면에 맞춘다. 대칭면 루프는 열어 둔다.

    QuadriFlow 출력은 구멍 가장자리에 겹친 정점(크랙)을 남기기도 해서(실측 2026-09-21, 갱스터: 3엣지 구멍 4개,
    겹친 정점 4개) 먼저 경계 정점을 용접한다. 짝수 구멍은 중심 정점을 세워 쿼드 부채로, 그 외는 면을 바로 만들어
    삼각화한 뒤 다시 합친다. 한 번에 안 닫히는 구멍이 있어 여러 번 돈다. 중심 정점 위치는 뒤따르는 슈링크랩이
    원본 표면으로 끌어온다."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    border_vertices = list({vertex for edge in bm.edges if len(edge.link_faces) == 1 for vertex in edge.verts})
    if border_vertices:
        # 대칭면 루프의 겹친 정점은 전형 엣지의 3% 정도 떨어져 있기도 하다(실측 2026-09-22, Y 대칭: 0.0017 대 0.05).
        # 그대로 두면 스무딩이 고정 경계 정점을 못 움직여 종횡비 60 슬리버가 남는다.
        lengths = sorted(edge.calc_length() for edge in bm.edges)
        median = lengths[len(lengths) // 2] if lengths else 0.0
        bmesh.ops.remove_doubles(bm, verts=border_vertices, dist=max(SNAP_TOLERANCE_RATIO * scale, BORDER_WELD_RATIO * median))
    plane_vertices: dict = {}
    for _ in range(REPAIR_ROUNDS):
        leftovers = []
        open_holes = False
        for loop in _boundary_loops(bm):
            if _loop_on_plane(loop, plane_axes, scale):
                tolerance = _plane_snap_tolerance(loop, scale)
                for edge in loop:
                    for vertex in edge.verts:
                        plane_vertices[vertex] = max(tolerance, plane_vertices.get(vertex, 0.0))
                continue
            open_holes = True
            ordered = _ordered_loop_vertices(loop)
            if ordered is not None and 3 <= len(ordered) <= HOLE_MAX_EDGES and _fill_loop(bm, ordered):
                continue
            leftovers.extend(loop)
        if not open_holes:
            break
        if leftovers:
            bmesh.ops.holes_fill(bm, edges=sorted(set(leftovers), key=lambda edge: edge.index), sides=0)
    odd = [face for face in bm.faces if len(face.verts) != 4]
    if odd:
        result = bmesh.ops.triangulate(bm, faces=odd)
        triangles = [face for face in result["faces"] if face.is_valid and len(face.verts) == 3]
        if triangles:
            bmesh.ops.join_triangles(
                bm, faces=triangles, cmp_seam=False, cmp_sharp=False, cmp_uvs=False,
                cmp_vcols=False, cmp_materials=False, angle_face_threshold=math.radians(40.0),
                angle_shape_threshold=math.radians(40.0),
            )
    _snap_plane_vertices({vertex: tolerance for vertex, tolerance in plane_vertices.items() if vertex.is_valid}, plane_axes)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _fill_loop(bm, ordered) -> bool:
    """단순 폐루프 하나를 면으로 닫는다. 짝수 6각 이상은 중심 정점 쿼드 부채, 나머지는 면 하나(뒤에서 삼각화·병합)."""
    count = len(ordered)
    try:
        if count >= 6 and count % 2 == 0:
            centre_co = ordered[0].co.copy()
            for vertex in ordered[1:]:
                centre_co += vertex.co
            centre = bm.verts.new(centre_co / count)
            for index in range(0, count, 2):
                bm.faces.new((centre, ordered[index], ordered[(index + 1) % count], ordered[(index + 2) % count]))
        else:
            bm.faces.new(ordered)
    except ValueError:
        return False  # 이미 있는 면과 겹치는 루프 — 일반 구멍 메우기에 맡긴다
    return True


def _snap_plane_vertices(plane_vertices: dict, plane_axes: tuple[str, ...]) -> None:
    """대칭면 루프 정점을 평면에 붙인다. 정점마다 가장 가까운 평면 하나에만 붙이고, 두 평면이 만나는 모서리는
    평면 쌍마다 교선에 가장 가까운 정점 하나만 두 평면에 붙인다.

    이웃한 두 정점을 모두 두 평면에 붙이면 교선 위에 엣지가 놓이고, 두 번 미러된 뒤 면 네 개가 그 엣지를 공유해
    비다양체가 된다(실측 2026-09-21, 옥탄트: 비다양체 엣지 12)."""
    components = ["XYZ".index(axis) for axis in plane_axes]
    corners: dict = {}
    for first in range(len(components)):
        for second in range(first + 1, len(components)):
            a, b = components[first], components[second]
            candidates = sorted(
                (
                    (abs(vertex.co[a]) ** 2 + abs(vertex.co[b]) ** 2, vertex, tolerance)
                    for vertex, tolerance in plane_vertices.items()
                    if abs(vertex.co[a]) < tolerance and abs(vertex.co[b]) < tolerance
                ),
                key=lambda item: item[0],
            )
            chosen = []
            for _, vertex, tolerance in candidates:
                if any((vertex.co - other.co).length < 2.0 * tolerance for other in chosen):
                    continue
                chosen.append(vertex)
                corners.setdefault(vertex, set()).update((a, b))
    for vertex, tolerance in plane_vertices.items():
        if vertex in corners:
            for component in corners[vertex]:
                vertex.co[component] = 0.0
            continue
        nearest = min(components, key=lambda component: abs(vertex.co[component]))
        if abs(vertex.co[nearest]) < tolerance:
            vertex.co[nearest] = 0.0


def _mirror(obj, axes: tuple[str, ...], scale: float) -> None:
    """양의 반쪽을 대칭면 기준으로 복제·용접해 닫힌 대칭 메시로 만든다."""
    modifier = obj.modifiers.new("ZZ_Mirror", "MIRROR")
    modifier.use_axis = tuple(axis in axes for axis in "XYZ")
    modifier.use_bisect_axis = (False, False, False)
    modifier.use_mirror_merge = True
    modifier.merge_threshold = SNAP_TOLERANCE_RATIO * scale
    _apply_modifiers(obj)


# --- 투영 ---------------------------------------------------------------------

def _shrinkwrap(obj, target_obj, limit: float, plane_axes: tuple[str, ...], scale: float, *, cancelled: CancelledCallback | None) -> None:
    """결과를 원본 표면에 붙여 복셀·QuadriFlow 가 뭉갠 디테일을 되찾는다.

    최근접점만 쓰면 접히는 공간에서 이웃 정점이 서로 다른 표면으로 끌려가 면이 교차하므로 노멀 방향 양방향
    투영을 먼저 하고, 빗나간 정점만 최근접점으로 붙인다. 그 뒤 스무딩과 재투영을 반복해 접힌 와이어를 편다.
    마지막으로 종횡비가 큰 슬리버 면 주변만 골라 다시 편다 — QuadriFlow 는 구멍 주변에 2mm 남짓한 엣지를 남기고
    이것이 종횡비 20 게이트에 걸린다(실측 2026-09-21, 갱스터 10만면: 22.2 → 7.6).
    대칭면 위 정점은 매 단계 뒤 평면으로 되돌려 미러 용접이 어긋나지 않게 한다."""
    import bmesh
    import bpy
    from mathutils.bvhtree import BVHTree

    tree = BVHTree.FromObject(target_obj, bpy.context.evaluated_depsgraph_get())
    # _repair_output 이 대칭면 루프 정점만 정확히 0.0 으로 스냅해 두므로 그 정점을 고정 대상으로 삼는다.
    # 슈링크랩·스무딩은 디폼만 하므로 정점 수와 순서가 유지된다.
    pinned = [
        (index, "XYZ".index(axis))
        for index, vertex in enumerate(obj.data.vertices)
        for axis in plane_axes
        if vertex.co["XYZ".index(axis)] == 0.0
    ]
    vertex_count = len(obj.data.vertices)

    def pin() -> None:
        """대칭면 정점은 평면으로 되돌리고, 투영·스무딩으로 평면을 넘어간 정점은 평면에서 멈춘다.
        넘어간 정점을 두면 미러 복제와 겹쳐 대칭이 깨진다(실측: 대칭 오차 0.07)."""
        if not plane_axes:
            return
        vertices = obj.data.vertices
        for index, component in pinned:
            vertices[index].co[component] = 0.0
        components = ["XYZ".index(axis) for axis in plane_axes]
        for vertex in vertices:
            for component in components:
                if vertex.co[component] < 0.0:
                    vertex.co[component] = 0.0
        obj.data.update()

    def project() -> None:
        before = [v.co.copy() for v in obj.data.vertices]
        modifier = obj.modifiers.new("ZZ_Shrinkwrap", "SHRINKWRAP")
        modifier.target = target_obj
        modifier.wrap_method = "PROJECT"
        modifier.use_negative_direction = True
        modifier.use_positive_direction = True
        modifier.project_limit = limit
        modifier.offset = 0.0
        _apply_modifiers(obj)
        if len(obj.data.vertices) != vertex_count:
            raise ValueError("슈링크랩 적용 뒤 정점 수가 달라져 대칭면 고정을 이어갈 수 없습니다.")
        for index, vertex in enumerate(obj.data.vertices):
            if (vertex.co - before[index]).length_squared < 1e-14:
                nearest = tree.find_nearest(vertex.co)
                if nearest[0] is not None:
                    vertex.co = nearest[0]
        obj.data.update()
        pin()

    def relax() -> None:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bmesh.ops.smooth_vert(bm, verts=bm.verts[:], factor=RELAX_FACTOR, use_axis_x=True, use_axis_y=True, use_axis_z=True)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        pin()

    def relax_slivers() -> None:
        for _ in range(SLIVER_ROUNDS):
            _check_cancelled(cancelled)
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            slivers = [face for face in bm.faces if _face_aspect(face) > SLIVER_ASPECT]
            if not slivers:
                bm.free()
                return
            # 슬리버 정점과 그 이웃 한 겹을 함께 펴야 짧은 엣지가 실제로 늘어난다
            ring = list({neighbour for face in slivers for vertex in face.verts for edge in vertex.link_edges for neighbour in edge.verts})
            bmesh.ops.smooth_vert(bm, verts=ring, factor=RELAX_FACTOR, use_axis_x=True, use_axis_y=True, use_axis_z=True)
            for vertex in ring:
                nearest = tree.find_nearest(vertex.co)
                if nearest[0] is not None:
                    vertex.co = nearest[0]
            bm.to_mesh(obj.data)
            bm.free()
            obj.data.update()
            pin()

    project()
    for _ in range(RELAX_ROUNDS):
        _check_cancelled(cancelled)
        relax()
        project()
    relax_slivers()


# --- 품질 측정 ---------------------------------------------------------------

def _mesh_data_from_object(obj) -> MeshData:
    mesh = obj.data
    vertices = tuple(tuple(float(c) for c in v.co) for v in mesh.vertices)
    faces = tuple(tuple(int(i) for i in polygon.vertices) for polygon in mesh.polygons)
    return MeshData(vertices, faces, frozenset())


def _bidirectional_distance(source: MeshData, output: MeshData, *, cancelled: CancelledCallback | None):
    """정점과 면 중심 표본에서 상대 표면까지의 거리 (출력→원본 최대·평균, 원본→출력 최대·평균)."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    source_tree = BVHTree.FromPolygons([Vector(v) for v in source.vertices], [tuple(f) for f in source.faces], all_triangles=False)
    output_tree = BVHTree.FromPolygons([Vector(v) for v in output.vertices], [tuple(f) for f in output.faces], all_triangles=False)
    max_out, mean_out = _distance_stats(_samples(output), source_tree, cancelled)
    max_src, mean_src = _distance_stats(_samples(source), output_tree, cancelled)
    return max_out, mean_out, max_src, mean_src


def _samples(mesh: MeshData, limit: int = 20000) -> list[Vector3]:
    points = list(mesh.vertices)
    for face in mesh.faces:
        count = len(face)
        points.append((
            sum(mesh.vertices[i][0] for i in face) / count,
            sum(mesh.vertices[i][1] for i in face) / count,
            sum(mesh.vertices[i][2] for i in face) / count,
        ))
    if len(points) <= limit:
        return points
    stride = len(points) / limit
    return [points[int(i * stride)] for i in range(limit)]


def _distance_stats(points, tree, cancelled) -> tuple[float, float]:
    from mathutils import Vector

    maximum = 0.0
    total = 0.0
    for index, point in enumerate(points):
        if index % 2048 == 0:
            _check_cancelled(cancelled)
        hit = tree.find_nearest(Vector(point))
        distance = hit[3] if hit[0] is not None else 0.0
        maximum = max(maximum, distance)
        total += distance
    return maximum, (total / len(points) if points else 0.0)


def _symmetry_error(vertices, axes: tuple[str, ...]) -> float:
    from mathutils import Vector
    from mathutils.kdtree import KDTree

    tree = KDTree(len(vertices))
    for index, vertex in enumerate(vertices):
        tree.insert(Vector(vertex), index)
    tree.balance()
    maximum = 0.0
    for axis in axes:
        component = "XYZ".index(axis)
        for vertex in vertices:
            mirrored = list(vertex)
            mirrored[component] = -mirrored[component]
            _, _, distance = tree.find(Vector(mirrored))
            maximum = max(maximum, distance)
    return maximum


def _face_aspect(face) -> float:
    lengths = [edge.calc_length() for edge in face.edges]
    return max(lengths) / max(min(lengths), 1.0e-12)


def _quad_aspect_stats(vertices, faces) -> tuple[float, float]:
    ratios = []
    for face in faces:
        points = [vertices[index] for index in face]
        lengths = [_distance(a, b) for a, b in zip(points, (*points[1:], points[0]))]
        shortest = min(lengths)
        longest = max(lengths)
        ratios.append(longest / shortest if shortest > 1.0e-12 else float("inf"))
    if not ratios:
        return float("inf"), float("inf")
    return max(ratios), sum(ratios) / len(ratios)


def _bbox_scale(vertices) -> float:
    return max(max(v[a] for v in vertices) - min(v[a] for v in vertices) for a in range(3))


def _distance(a: Vector3, b: Vector3) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _check_cancelled(cancelled: CancelledCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise RemeshCancelled("작업이 취소되었습니다.")


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(max(0.0, min(1.0, fraction)), message)
