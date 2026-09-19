from __future__ import annotations

import bpy

from .operators import CLASSES as OPERATOR_CLASSES
from .panel import CLASSES as PANEL_CLASSES
from .properties import Zzamjak3DRemesherProperties


CLASSES = (
    Zzamjak3DRemesherProperties,
    *OPERATOR_CLASSES,
    *PANEL_CLASSES,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.zzamjak_3d_remesher = bpy.props.PointerProperty(type=Zzamjak3DRemesherProperties)


def unregister():
    if hasattr(bpy.types.Scene, "zzamjak_3d_remesher"):
        del bpy.types.Scene.zzamjak_3d_remesher
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
