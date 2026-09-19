"""격리 Blender worker에서 독립 튜브와 큐브의 결합 결과를 검증한다."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import time

import bpy


sys.path.insert(0, str(Path(os.environ["REMESHER_DEV_ROOT"])))
from tests.test_components_topology import _cube, _join, _tube


MODULE = "bl_ext.user_default.zzamjak_3d_remesher"
operators = importlib.import_module(MODULE + ".addon.operators")

source_data = _join(_tube(-4.0), _cube(3.0))
mesh = bpy.data.meshes.new("독립 삼각 튜브와 큐브")
mesh.from_pydata(source_data.vertices, [], source_data.faces)
mesh.update()
source = bpy.data.objects.new("독립 삼각 튜브와 큐브", mesh)
bpy.context.collection.objects.link(source)
bpy.context.view_layer.objects.active = source
source.select_set(True)

props = bpy.context.scene.zzamjak_3d_remesher
props.topology_mode = "STRUCTURED"
props.target_quad_count = 768
before = operators._source_input_fingerprint(source, bpy.context.scene)
engine_input, warnings = operators._build_engine_input(bpy.context, source)
assert len(engine_input.mesh.faces) == len(source_data.faces)
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 90.0
    while job.process.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("분리 표면 worker가 90초 안에 끝나지 않았습니다.")
        time.sleep(0.05)
    payload = operators._read_job_result(job)
    assert payload.get("ok"), payload
    result = operators._deserialize_remesh_result(payload["result"])
    assert abs(result.quality.actual_quad_count - 768) / 768 <= 0.05
    assert result.quality.quad_ratio == 1.0
    assert result.quality.non_manifold_edge_count == 0

    vertex_faces = {index: set() for index in range(len(result.mesh.vertices))}
    for face_index, face in enumerate(result.mesh.faces):
        for vertex in face:
            vertex_faces[vertex].add(face_index)
    pending = set(range(len(result.mesh.faces)))
    components = []
    while pending:
        stack = [pending.pop()]
        faces = set(stack)
        while stack:
            face_index = stack.pop()
            for vertex in result.mesh.faces[face_index]:
                for adjacent in vertex_faces[vertex] & pending:
                    pending.remove(adjacent)
                    faces.add(adjacent)
                    stack.append(adjacent)
        components.append(faces)
    assert len(components) == 2, len(components)
    assert operators._source_input_fingerprint(source, bpy.context.scene) == before
    print(f"분리 표면 worker 검증 통과: {result.quality.actual_quad_count}쿼드, 독립 컴포넌트 2개, 원본 보존")
finally:
    operators._cleanup_job_files(job)
