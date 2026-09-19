"""별도 개발 GUI 프로세스에서 실제 invoke/timer 완료와 취소를 검증한다."""
import importlib
import json
from pathlib import Path
import time
import traceback

import bpy

MODULE="bl_ext.user_default.zzamjak_3d_remesher"
operators=importlib.import_module(f"{MODULE}.addon.operators")
root=Path(__file__).resolve().parents[1]
report_path=root/'dist/gui_verification.json'
report_path.parent.mkdir(exist_ok=True)
state={"stage":"start","deadline":time.monotonic()+90,"checks":[]}


def finish(ok,error=""):
    operators.cancel_active_job()
    report_path.write_text(json.dumps({"ok":ok,"checks":state["checks"],"error":error},ensure_ascii=False,indent=2),encoding='utf-8')
    print("GUI 모달 검증 결과:",ok,error,flush=True)
    bpy.ops.wm.quit_blender()


def tick():
    try:
        if time.monotonic()>state["deadline"]:
            raise AssertionError("GUI 모달 검증 시간이 초과되었습니다.")
        props=bpy.context.scene.zzamjak_3d_remesher
        if state["stage"]=="start":
            bpy.ops.mesh.primitive_plane_add()
            state["source"]=bpy.context.object
            state["source"].name="모달검증원본"
            props.target_quad_count=64
            props.symmetry_x=props.symmetry_y=props.symmetry_z=False
            result=bpy.ops.object.zzamjak_3d_remesher_run('INVOKE_DEFAULT')
            assert result=={'RUNNING_MODAL'},result
            assert props.run_busy
            state["stage"]="complete"
        elif state["stage"]=="complete" and not props.run_busy:
            assert operators._ACTIVE_JOB is None
            assert bpy.context.object is not state["source"],props.last_report
            assert all(len(f.vertices)==4 for f in bpy.context.object.data.polygons)
            state["checks"].append("실제 GUI invoke/timer 경로에서 결과 생성")
            state["objects"]=len(bpy.data.objects)
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            source=state["source"]
            source.select_set(True)
            bpy.context.view_layer.objects.active=source
            props.target_quad_count=5000
            assert bpy.ops.object.zzamjak_3d_remesher_run('INVOKE_DEFAULT')=={'RUNNING_MODAL'}
            operators._request_job_cancel(operators._ACTIVE_JOB,terminate=True)
            state["stage"]="cancel"
        elif state["stage"]=="cancel" and not props.run_busy:
            assert operators._ACTIVE_JOB is None
            assert len(bpy.data.objects)==state["objects"]
            assert "취소" in props.last_report,props.last_report
            state["checks"].append("실제 GUI 모달 작업 취소와 결과 미적용")
            props.target_quad_count=64
            assert bpy.ops.object.zzamjak_3d_remesher_run('INVOKE_DEFAULT')=={'RUNNING_MODAL'}
            state["source"].data.vertices[0].co.x-=.1
            state["stage"]="changed"
        elif state["stage"]=="changed" and not props.run_busy:
            assert operators._ACTIVE_JOB is None
            assert len(bpy.data.objects)==state["objects"]
            assert "변경" in props.last_report,props.last_report
            state["checks"].append("계산 도중 입력 변경 시 결과 적용 거부")
            finish(True)
            return None
        return .15
    except Exception:
        finish(False,traceback.format_exc())
        return None


assert not bpy.app.background,"이 검사는 별도 GUI Blender로 실행해야 합니다."
bpy.app.timers.register(tick,first_interval=.5)
