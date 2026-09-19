"""격리 프로필에서 큰 입력의 실제 worker·취소·원본 보존을 검증한다."""

from __future__ import annotations

import importlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import bpy


def main():
    module_name = "bl_ext.user_default.zzamjak_3d_remesher"
    operators = importlib.import_module(module_name + ".addon.operators")
    large = importlib.import_module(module_name + ".addon.large_mesh")
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1)
    source = bpy.context.active_object
    assert len(source.data.polygons) > 10000
    props = bpy.context.scene.zzamjak_3d_remesher
    props.target_quad_count = 1024
    props.symmetry_x = True
    props.symmetry_y = True
    props.symmetry_z = True
    before = operators._source_input_fingerprint(source, bpy.context.scene)
    engine_input, warnings = operators._build_engine_input(bpy.context, source)
    assert large.needs_preprocessing(engine_input.mesh)
    # 명시 특징선의 양 끝점은 축소 중 이동하거나 사라지면 안 된다.
    hard_edges = frozenset(tuple(sorted(edge.vertices)) for edge in list(source.data.edges)[::1000])
    protected_mesh = replace(engine_input.mesh, hard_edges=hard_edges)
    proxy, _ = large._build_decimated_proxy(protected_mesh, 4000, progress=None, cancelled=None)
    proxy_points = set(proxy.vertices)
    for index in {vertex for edge in hard_edges for vertex in edge}:
        assert protected_mesh.vertices[index] in proxy_points, "축소 중 보호 정점이 사라졌습니다."
    assert len(proxy.faces) < len(protected_mesh.faces)
    started = time.monotonic()
    job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
    try:
        assert "--large" in job.process.args
        assert "--factory-startup" in job.process.args
        assert (job.temp_dir / "profile" / "config").is_dir()
        while job.process.poll() is None:
            if time.monotonic() - started > 240:
                raise AssertionError("큰 메시 작업이 240초 안에 완료되지 않았습니다.")
            time.sleep(0.1)
        payload = operators._read_job_result(job)
        assert payload.get("ok"), payload
        result = operators._deserialize_remesh_result(payload["result"])
        assert result.quality.quad_ratio == 1.0
        assert result.quality.degenerate_face_count == 0
        assert result.quality.non_manifold_edge_count == 0
        assert result.quality.symmetry_error < 1e-6
        assert result.quality.max_surface_error <= 0.08
        assert before == operators._source_input_fingerprint(source, bpy.context.scene)
        report = {
            "source_faces": len(source.data.polygons),
            "result_quads": result.quality.actual_quad_count,
            "seconds": time.monotonic() - started,
            "quality": vars(result.quality),
            "source_preserved": True,
        }
    finally:
        operators._cleanup_job_files(job)
    assert not job.temp_dir.exists()

    cancel_job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
    operators._request_job_cancel(cancel_job, terminate=True)
    operators._cleanup_job_files(cancel_job)
    assert cancel_job.process.poll() is not None
    assert not cancel_job.temp_dir.exists()
    assert before == operators._source_input_fingerprint(source, bpy.context.scene)
    report["cancel_cleanup"] = True
    report["ok"] = True
    destination = Path(os.environ["REMESHER_DEV_ROOT"]) / "dist" / "large-mesh-verification.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    print("큰 메시 worker 및 취소 검증 통과")


if __name__ == "__main__":
    main()
