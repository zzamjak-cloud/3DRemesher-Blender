bl_info = {
    "name": "3D Remesher",
    "author": "zzamjak-cloud",
    "version": (0, 4, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > 3D Remesher",
    "description": "대칭과 밀도를 제어하는 적응형 쿼드 리토폴로지",
    "category": "Mesh",
}

from .addon.registration import register, unregister
