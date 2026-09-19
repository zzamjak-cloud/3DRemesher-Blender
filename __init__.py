bl_info = {
    "name": "3D Remesher",
    "author": "zzamjak-cloud",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > 3D Remesher",
    "description": "특징선을 보존하는 실험 쿼드 메시 생성 도구",
    "category": "Mesh",
}

from .addon.registration import register, unregister
