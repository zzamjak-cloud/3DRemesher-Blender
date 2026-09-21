# QuadriFlow 자식 프로세스 — 부모 Blender 가 멈추지 않도록 별도 프로세스에서 돈다.
#
# 부모가 넘긴 .blend 에서 메시 하나를 읽어 QuadriFlow 를 걸고 결과를 다른 .blend 로 쓴다.
# `blender --background --factory-startup --python 이파일 -- 입력 출력 목표면수 경계보존 시드` 로 실행되므로
# 패키지 상대 임포트를 쓰지 않는다. 경계보존이 "1" 이면 열린 경계(대칭 절단면)를 그대로 유지한다.
# QuadriFlow 의 자체 대칭 모드는 쓰지 않는다 — 대칭면을 따라 큰 구멍을 남긴다(실측 2026-09-21).
import sys

import bpy


def main(argv) -> int:
    src, dst, target, preserve_boundary, seed = argv[0], argv[1], int(argv[2]), argv[3] == "1", int(argv[4])
    with bpy.data.libraries.load(src) as (data_from, data_to):
        data_to.meshes = data_from.meshes[:1]
    if not data_to.meshes:
        return 2
    obj = bpy.data.objects.new("ZZ_QuadriFlow", data_to.meshes[0])
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    before = len(obj.data.polygons)
    bpy.ops.object.quadriflow_remesh(
        mode="FACES",
        target_faces=target,
        use_mesh_symmetry=False,
        use_preserve_sharp=True,
        use_preserve_boundary=preserve_boundary,
        seed=seed,
    )
    if len(obj.data.polygons) == before:
        return 3  # 조용히 아무것도 하지 않았다 — 부모가 다른 시드로 넘어간다
    bpy.data.libraries.write(dst, {obj.data}, fake_user=True)
    return 0


if __name__ == "__main__":
    arguments = sys.argv[sys.argv.index("--") + 1 :]
    sys.exit(main(arguments))
