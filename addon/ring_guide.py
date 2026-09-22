"""표면을 클릭해 팔·다리 단면 LOOP 가이드를 만드는 오퍼레이터와 가이드 오브젝트 속성.

손으로 원을 그리면 크기·기울기가 맞지 않아 절단 링이 조각으로 잡히거나(0.5.0 좀비 실측) 발등·가슴을 함께
지난다. 클릭 지점의 두께와 최소 둘레 단면으로 축을 정해 실제 단면 곡선을 커브로 만들고, 절단면 양쪽 둘레 비율을
바로 계산해 패널에 보여 준다. 오프셋을 바꾸면 축을 따라 옮겨 다시 자른다.
"""

from __future__ import annotations

import math

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector

from .blender_adapter import mesh_data_from_object
from .ring_geometry import RIM_OK_RATIO, SectionMesh, estimate_ring, resample_loop, rim_ratio, slice_ring

GUIDE_PREFIX = "REMESH_GUIDE_ring"
_refreshing = False
_pending_refresh: set[str] = set()


def _on_offset_changed(self, context):
    """update 콜백 안에서 커브 ID 데이터를 다시 쓰면 뎁스그래프 평가와 겹치므로 타이머로 미뤄 적용한다."""
    if _refreshing:
        return
    name = self.id_data.name
    if name in _pending_refresh:
        return
    _pending_refresh.add(name)

    def apply():
        _pending_refresh.discard(name)
        guide = bpy.data.objects.get(name)
        if guide is not None and guide.type == "CURVE" and guide.zzamjak_ring_guide.is_ring:
            refresh_ring_guide(guide)
        return None

    bpy.app.timers.register(apply, first_interval=0.0)


class ZzamjakRingGuideProperties(bpy.types.PropertyGroup):
    is_ring: bpy.props.BoolProperty(name="클릭 링 가이드", default=False)
    source: bpy.props.PointerProperty(name="원본 메시", type=bpy.types.Object)
    center: bpy.props.FloatVectorProperty(name="중심", size=3, subtype="XYZ")
    axis: bpy.props.FloatVectorProperty(name="축", size=3, subtype="XYZ")
    hit: bpy.props.FloatVectorProperty(name="클릭 지점", size=3, subtype="XYZ")
    radius: bpy.props.FloatProperty(name="반지름", default=0.0)
    offset: bpy.props.FloatProperty(
        name="축 오프셋",
        description="축을 따라 링을 옮기고 그 위치의 단면을 다시 자릅니다",
        default=0.0,
        soft_min=-1.0,
        soft_max=1.0,
        step=1,
        precision=3,
        update=_on_offset_changed,
    )
    ratio: bpy.props.FloatProperty(name="둘레 비율", default=0.0)
    status: bpy.props.StringProperty(name="상태", default="")


def half_width_for(source, scene) -> float:
    """quadriflow_path 와 같은 기준: 출력 엣지 길이의 절반 (면적 / 목표 쿼드 수)."""
    area = sum(polygon.area for polygon in source.data.polygons)
    target = max(1, scene.zzamjak_3d_remesher.target_quad_count)
    return 0.5 * math.sqrt(area / target)


def create_ring_guide(context, source, hit_local, normal_local):
    """원본 로컬 좌표의 클릭 지점·법선으로 링 가이드 커브를 만든다. 실패하면 (None, 사유)."""
    mesh = SectionMesh(mesh_data_from_object(source))
    scale = _local_scale(source)
    # 클릭 점이 표면에서 조금 떠 있으면 안쪽 레이가 자기 표면을 먼저 맞아 두께가 0 에 가까워지므로 표면에 붙인다
    found, location, normal, _index = source.closest_point_on_mesh(Vector(hit_local))
    if found:
        hit_local, normal_local = tuple(location), tuple(normal)
    estimate = estimate_ring(mesh, tuple(hit_local), tuple(normal_local), scale)
    if estimate is None:
        return None, "클릭 지점 주변에서 닫힌 단면을 찾지 못했습니다."
    global _refreshing
    curve = bpy.data.curves.new(GUIDE_PREFIX, "CURVE")
    curve.dimensions = "3D"
    guide = bpy.data.objects.new(GUIDE_PREFIX, curve)
    _scene_collection_for(context, source).objects.link(guide)
    guide.matrix_world = source.matrix_world.copy()
    guide.show_in_front = True
    props = guide.zzamjak_ring_guide
    _refreshing = True  # offset 대입도 update 콜백을 부르므로 초기화 동안은 막는다
    try:
        props.is_ring = True
        props.source = source
        props.center = estimate.center
        props.axis = estimate.axis
        props.hit = tuple(hit_local)
        props.radius = estimate.radius
        props.offset = 0.0
    finally:
        _refreshing = False
    refresh_ring_guide(guide, mesh=mesh)
    return guide, ""


def _local_scale(source) -> float:
    """원본 로컬 바운딩박스의 가장 긴 변. 단면 계산이 로컬 좌표라 월드 크기(dimensions)를 쓰면 스케일만큼 어긋난다."""
    corners = [tuple(c) for c in source.bound_box]
    extent = max(max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3))
    return extent if extent > 0.0 else 1.0


def _scene_collection_for(context, source):
    """원본이 속한 컬렉션 중 현재 씬에 링크된 것. 없으면 현재 컬렉션 — 씬 밖 컬렉션에 넣으면 보이지도, 검사되지도 않는다."""
    scene_collections = {context.scene.collection.name, *(c.name for c in context.scene.collection.children_recursive)}
    for collection in source.users_collection:
        if collection.name in scene_collections:
            return collection
    return context.collection if context.collection is not None else context.scene.collection


def refresh_ring_guide(guide, *, mesh=None) -> bool:
    """오프셋 위치의 단면을 다시 잘라 커브 점과 둘레 비율을 갱신한다."""
    props = guide.zzamjak_ring_guide
    source = props.source
    if source is None or source.type != "MESH":
        props.status = "원본 메시를 찾을 수 없습니다"
        return False
    if mesh is None:
        mesh = SectionMesh(mesh_data_from_object(source))
    axis = tuple(props.axis)
    center = tuple(props.center[i] + axis[i] * props.offset for i in range(3))
    hit = tuple(props.hit[i] + axis[i] * props.offset for i in range(3))
    loop = slice_ring(mesh, center, axis, hit, props.radius)
    if loop is None:
        props.status = "이 위치에는 닫힌 단면이 없습니다"
        props.ratio = 0.0
        return False
    points = resample_loop(loop)
    _write_points(guide.data, points)
    half_width = half_width_for(source, bpy.context.scene)
    props.ratio = rim_ratio(mesh, center, axis, hit, props.radius, half_width)
    props.status = "사용 가능" if props.ratio >= RIM_OK_RATIO else "단면 급변: 위치를 옮겨 주세요"
    return True


def _write_points(curve, points) -> None:
    curve.splines.clear()
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for point, target in zip(points, spline.points):
        target.co = (point[0], point[1], point[2], 1.0)
    spline.use_cyclic_u = True


def ring_guides(scene):
    return [obj for obj in scene.objects if obj.type == "CURVE" and obj.zzamjak_ring_guide.is_ring]


def remove_guide(guide) -> None:
    """가이드 오브젝트와 다른 사용자가 없는 커브 데이터를 함께 지운다."""
    data = guide.data
    bpy.data.objects.remove(guide, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.curves.remove(data)


class ZJREMESH_OT_add_ring_guide(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_add_ring_guide"
    bl_label = "클릭으로 링 가이드 추가"
    bl_description = "메시 표면을 클릭한 자리에 축에 수직인 단면 LOOP 가이드를 만듭니다. Ctrl+Z 로 마지막 링을 취소하고 우클릭이나 ESC 로 끝냅니다"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH" and context.mode == "OBJECT"

    def invoke(self, context, event):
        if context.area is None or context.area.type != "VIEW_3D":
            self.report({"ERROR"}, "3D 뷰포트에서 실행해야 합니다.")
            return {"CANCELLED"}
        self._source_name = context.active_object.name
        self._area = context.area  # 마우스가 영역 밖이면 context.area 가 None 이라 헤더 정리에 쓴다
        self._created = []
        context.window_manager.modal_handler_add(self)
        self._update_header()
        return {"RUNNING_MODAL"}

    def _update_header(self):
        try:
            self._area.header_text_set(
                f"링 가이드 {len(self._created)}개 추가 — 좌클릭: 추가, Ctrl+Z/Backspace: 마지막 취소, 우클릭/ESC: 종료"
            )
        except (AttributeError, ReferenceError):
            pass

    def _finish(self):
        try:
            self._area.header_text_set(None)
        except (AttributeError, ReferenceError):
            pass

    def modal(self, context, event):
        if event.type in {"RIGHTMOUSE", "ESC"}:
            self._finish()
            return {"FINISHED"}
        undo_key = (event.type == "Z" and event.ctrl) or event.type == "BACK_SPACE"
        if undo_key and event.value == "PRESS":
            # 모달 중 전역 언도가 끼어들면 원본까지 되돌아갈 수 있어 여기서 삼키고, 이 세션에서 만든 마지막 링만 지운다
            while self._created:
                guide = bpy.data.objects.get(self._created.pop())
                if guide is not None:
                    remove_guide(guide)
                    self.report({"INFO"}, "마지막 링 가이드를 취소했습니다.")
                    break
            self._update_header()
            return {"RUNNING_MODAL"}
        if undo_key:
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            source = bpy.data.objects.get(self._source_name)
            if source is None:
                self._finish()
                return {"CANCELLED"}
            hit = _raycast(context, event, source)
            if hit is None:
                self.report({"WARNING"}, "메시 표면을 클릭해 주세요.")
                return {"RUNNING_MODAL"}
            guide, reason = create_ring_guide(context, source, *hit)
            if guide is None:
                self.report({"WARNING"}, reason)
            else:
                self._created.append(guide.name)
                props = guide.zzamjak_ring_guide
                self.report({"INFO"}, f"{guide.name}: 반지름 {props.radius:.3f}, 둘레 비율 {props.ratio:.2f} — {props.status}")
            self._update_header()
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}


class ZJREMESH_OT_check_ring_guides(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_check_ring_guides"
    bl_label = "링 가이드 다시 검사"
    bl_description = "모든 클릭 링 가이드의 단면과 둘레 비율을 현재 설정으로 다시 계산합니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        guides = ring_guides(context.scene)
        if not guides:
            self.report({"INFO"}, "클릭으로 만든 링 가이드가 없습니다.")
            return {"CANCELLED"}
        bad = [guide.name for guide in guides if not refresh_ring_guide(guide) or guide.zzamjak_ring_guide.ratio < RIM_OK_RATIO]
        if bad:
            self.report({"WARNING"}, f"위치 조정이 필요한 가이드: {', '.join(bad)}")
        else:
            self.report({"INFO"}, f"링 가이드 {len(guides)}개 모두 사용 가능합니다.")
        return {"FINISHED"}


def _raycast(context, event, source):
    """마우스 위치에서 원본 메시로 레이를 쏘아 (로컬 히트 점, 로컬 면 법선) 을 돌려준다."""
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None or region.type != "WINDOW":
        return None  # 사이드바 위 클릭은 뷰포트 레이가 아니다
    coord = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    inverse = source.matrix_world.inverted()
    local_origin = inverse @ origin
    local_direction = (inverse.to_3x3() @ direction).normalized()
    result, location, normal, _index = source.ray_cast(local_origin, local_direction)
    if not result:
        return None
    return tuple(location), tuple(normal)


class ZJREMESH_OT_remove_ring_guide(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_remove_ring_guide"
    bl_label = "링 가이드 삭제"
    bl_description = "이 링 가이드를 지웁니다"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(name="가이드 이름", options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        guide = bpy.data.objects.get(self.name)
        if guide is None or guide.type != "CURVE" or not guide.zzamjak_ring_guide.is_ring:
            self.report({"WARNING"}, f"링 가이드를 찾을 수 없습니다: {self.name}")
            return {"CANCELLED"}
        remove_guide(guide)
        return {"FINISHED"}


class ZJREMESH_OT_clear_ring_guides(bpy.types.Operator):
    bl_idname = "object.zzamjak_3d_remesher_clear_ring_guides"
    bl_label = "링 가이드 모두 삭제"
    bl_description = "이 씬의 클릭 링 가이드를 모두 지웁니다"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        guides = ring_guides(context.scene)
        if not guides:
            self.report({"INFO"}, "클릭으로 만든 링 가이드가 없습니다.")
            return {"CANCELLED"}
        for guide in guides:
            remove_guide(guide)
        self.report({"INFO"}, f"링 가이드 {len(guides)}개를 지웠습니다.")
        return {"FINISHED"}


CLASSES = (
    ZzamjakRingGuideProperties,
    ZJREMESH_OT_add_ring_guide,
    ZJREMESH_OT_check_ring_guides,
    ZJREMESH_OT_remove_ring_guide,
    ZJREMESH_OT_clear_ring_guides,
)


def register_properties() -> None:
    bpy.types.Object.zzamjak_ring_guide = bpy.props.PointerProperty(type=ZzamjakRingGuideProperties)


def unregister_properties() -> None:
    if hasattr(bpy.types.Object, "zzamjak_ring_guide"):
        del bpy.types.Object.zzamjak_ring_guide
