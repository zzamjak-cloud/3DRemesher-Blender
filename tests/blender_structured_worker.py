from __future__ import annotations

import importlib
import time
from collections import Counter

import bpy


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")

bpy.ops.mesh.primitive_cube_add()
source = bpy.context.object
props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "AUTO"
props.target_quad_count = 384
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 30.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("격자 worker가 30초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert result.quality.actual_quad_count == 384
    assert not any("실험 엔진" in warning for warning in result.warnings)
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    edge_counts = Counter()
    for face in result.mesh.faces:
        for index, vertex in enumerate(face):
            edge_counts[tuple(sorted((vertex, face[(index + 1) % 4])))] += 1
    assert all(count == 2 for count in edge_counts.values())
    print("격자 worker 검증 통과: 384쿼드, 추가 폴백 없음, 원본 보존")
finally:
    operators._cleanup_job_files(job)
