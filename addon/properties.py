from __future__ import annotations

import bpy


class Zzamjak3DRemesherProperties(bpy.types.PropertyGroup):
    target_quad_count: bpy.props.IntProperty(
        name="목표 쿼드 수",
        description="실험 엔진이 가장 가까운 분할 수로 맞추려고 시도할 목표 쿼드 개수",
        default=5000,
        min=4,
        soft_max=100000,
    )
    symmetry_x: bpy.props.BoolProperty(name="X", default=False)
    symmetry_y: bpy.props.BoolProperty(name="Y", default=False)
    symmetry_z: bpy.props.BoolProperty(name="Z", default=False)
    hard_edge_angle: bpy.props.FloatProperty(
        name="하드 엣지 각도",
        description="이 각도 이상 꺾인 엣지를 흐름 보존 후보로 취급합니다",
        subtype="ANGLE",
        default=0.7853981633974483,
        min=0.0,
        max=3.141592653589793,
    )
    density_attribute_name: bpy.props.StringProperty(
        name="밀도 컬러 속성",
        description="점 도메인 FLOAT_COLOR 속성 이름",
        default="remesh_density",
    )
    density_scale: bpy.props.FloatProperty(
        name="밀도 배율",
        description="밀도 컬러 속성의 붉은 채널 값을 엔진 입력으로 전달할 때 곱할 배율",
        default=1.0,
        min=0.01,
        soft_max=10.0,
    )
    run_busy: bpy.props.BoolProperty(name="실행 중", default=False)
    run_progress: bpy.props.FloatProperty(name="진행률", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    run_progress_message: bpy.props.StringProperty(name="진행 상태", default="대기 중")
    last_report: bpy.props.StringProperty(name="최근 상태", default="아직 실행하지 않았습니다.")
