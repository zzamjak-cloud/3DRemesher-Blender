from __future__ import annotations

import bpy
from bpy.app.handlers import persistent

from .operators import cancel_active_job
from .operators import CLASSES as OPERATOR_CLASSES
from .panel import CLASSES as PANEL_CLASSES
from .properties import Zzamjak3DRemesherProperties
from .ring_guide import CLASSES as RING_GUIDE_CLASSES
from .ring_guide import register_properties as register_ring_guide_properties
from .ring_guide import unregister_properties as unregister_ring_guide_properties


CLASSES = (
    Zzamjak3DRemesherProperties,
    *OPERATOR_CLASSES,
    *RING_GUIDE_CLASSES,
    *PANEL_CLASSES,
)


@persistent
def _cancel_job_before_load(_dummy):
    cancel_active_job()


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.zzamjak_3d_remesher = bpy.props.PointerProperty(type=Zzamjak3DRemesherProperties)
    register_ring_guide_properties()
    if _cancel_job_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_cancel_job_before_load)


def unregister():
    cancel_active_job()
    if _cancel_job_before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_cancel_job_before_load)
    if hasattr(bpy.types.Scene, "zzamjak_3d_remesher"):
        del bpy.types.Scene.zzamjak_3d_remesher
    unregister_ring_guide_properties()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
