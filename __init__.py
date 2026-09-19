bl_info = {
    "name": "3D Remesher",
    "author": "zzamjak-cloud",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > 3D Remesher",
    "description": "게임 메시용 자동 리토폴로지 준비 도구",
    "category": "Mesh",
}

from .addon.registration import register, unregister
