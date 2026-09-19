from __future__ import annotations

import bpy


class Zzamjak3DRemesherProperties(bpy.types.PropertyGroup):
    topology_mode: bpy.props.EnumProperty(
        name="토폴로지 생성 방식",
        description="격자 경로를 우선 사용하거나 특정 경로만 실행합니다",
        items=(
            ("AUTO", "자동", "지원되는 형상에는 연속 격자를 사용하고 나머지는 실험 엔진 사용 사실을 알립니다"),
            ("STRUCTURED", "격자 전용", "격자 배치를 만들 수 없는 형상은 결과를 생성하지 않습니다"),
            ("LEGACY", "실험 엔진", "기존 적응형 삼각 패치 엔진을 사용합니다"),
        ),
        default="AUTO",
    )
    target_quad_count: bpy.props.IntProperty(
        name="목표 쿼드 수",
        description="격자·위상 제약 안에서 맞추려고 시도할 쿼드 개수",
        default=5000,
        min=4,
        soft_max=100000,
    )
    symmetry_x: bpy.props.BoolProperty(name="X", description="로컬 X 양의 축을 기준으로 절단 후 반대쪽을 미러링합니다", default=False)
    symmetry_y: bpy.props.BoolProperty(name="Y", description="로컬 Y 양의 축을 기준으로 절단 후 반대쪽을 미러링합니다", default=False)
    symmetry_z: bpy.props.BoolProperty(name="Z", description="로컬 Z 양의 축을 기준으로 절단 후 반대쪽을 미러링합니다", default=False)
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
        description="점 도메인 FLOAT_COLOR 속성 이름. 없으면 균일 밀도 1.0을 사용합니다",
        default="remesh_density",
    )
    density_scale: bpy.props.FloatProperty(
        name="밀도 대비",
        description="밝기 차이가 지역별 쿼드 밀도에 미치는 영향(1=기본)",
        default=1.0,
        min=0.05,
        soft_max=10.0,
    )
    run_busy: bpy.props.BoolProperty(name="실행 중", default=False)
    run_progress: bpy.props.FloatProperty(name="진행률", default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    run_progress_message: bpy.props.StringProperty(name="진행 상태", default="대기 중")
    last_report: bpy.props.StringProperty(name="최근 상태", default="아직 실행하지 않았습니다.")
