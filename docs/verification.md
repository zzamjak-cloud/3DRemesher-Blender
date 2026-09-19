# 실험 엔진 검증

2026-09-19, macOS의 실제 Blender 5.2.0 LTS에서 0.2.0을 검증했습니다. 이 결과는 첫 쿼드 생성 엔진의 기능 검사이며 전문적인 게임용 엣지 루프 품질을 보증하지 않습니다.

## 통과한 검사

| 검사 | 결과 |
| --- | --- |
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | 31개 통과 |
| `python3 scripts/verify_project.py` | 파일·메타데이터·Python 정적 검사 통과 |
| `bash -n scripts/dev_run.sh` | 셸 구문 검사 통과 |
| `./scripts/dev_run.sh --background --python tests/blender_smoke.py --python tests/blender_engine.py` | 실제 Blender 등록·연동 및 별도 프로세스 검사 통과 |
| `./scripts/dev_run.sh --background --python tests/blender_demo.py` | 실제 엔진 실행과 원본 보존, 비교용 `.blend` 저장 통과 |
| `python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender` | 공식 소스 검증·빌드·ZIP 검증 통과 |
| ZIP 무결성·현재 소스와 바이트 일치 | 통과 |

단위 검사는 열린 평면과 닫힌 큐브, 오목 다각형의 양방향 와인딩, 특징선 보존, 가이드에 따른 삼각형 병합 변화, 결정적 출력, 목표 수 제한, 취소 및 잘못된 입력 거부를 확인합니다.

Blender 검사는 전용 USER 리소스와 소스 링크, 활성화·등록·해제, 가이드 좌표 변환, 닫힌 POLY와 BEZIER 가이드, 밀도 데이터 보존, 결과 오브젝트 생성, 실패 시 원본·선택·데이터 보존을 확인합니다. 별도 프로세스 검사는 실제 JSON 입출력, 결과 역직렬화, 취소와 임시 파일 정리를 확인합니다. 테스트가 의도적으로 유발한 오류는 콘솔에 표시됩니다.

개발 프로필은 `~/Library/Application Support/Blender/3DRemesherDev/5.2/`입니다. 일상 프로필에 애드온을 설치하거나 설정을 저장하지 않았습니다.

## 비교용 데모

파일: `dist/3DRemesher_Prototype.blend`

| 측정 | 결과 |
| --- | --- |
| 입력 | 구의 삼각형 80개 |
| 목표 / 실제 | 200 / 240쿼드 |
| 쿼드 비율 | 100% |
| 경계 / 비다양체 엣지 / 퇴화 면 | 0 / 0 / 0 |
| 최대 / 평균 종횡비 | 2.057 / 1.859 |
| 원본 정점·면 변경 | 없음 |

종횡비는 가장 긴 엣지와 가장 짧은 엣지의 비율입니다. 표면 오차나 변형 품질 전체를 나타내지 않습니다. 목표보다 많은 결과는 면 감소 기능이 없는 현재 엔진의 한계이며 보고서에도 경고합니다. 데모 프로세스는 종료 코드 0이지만 Blender 종료 시 약 0.023 MB의 미해제 메모리 진단을 출력했습니다. 원인은 아직 분리하지 않았습니다.

## 설치용 산출물

- 파일: `dist/zzamjak_3d_remesher-v0.2.0.zip`
- 크기: 36,650 바이트
- SHA-256: `e781113c052214afe9bc428c60480def5060615416fe9eb084c6979793daf42e`
- 포함 내용: manifest, GPL 전문, 루트 등록 모듈, 엔진과 worker를 포함한 `addon/*.py`
- 개발 스크립트·테스트·문서·CI·캐시 제외

해시는 이번 생성본의 값이며 재빌드 시 ZIP 메타데이터에 따라 달라질 수 있습니다.

## 아직 검증하지 않은 범위

- Windows 실제 실행: PowerShell 5.1용 UTF-8 BOM, ASCII 배치 파일, Junction의 기존 데이터 보호, 인자·종료 코드 전달을 정적으로 검사했습니다. 이 환경에는 PowerShell 파서가 없어 구문 실행 및 런타임 검증은 수행하지 않았습니다.
- Blender 4.2 실제 실행: 최소 버전 선언만 있으며 현재 실행 검증은 5.2.0입니다.
- GUI 버튼·진행률·Esc 키의 실제 사용자 조작: 개발 창의 데모 파일 로드는 확인했지만 GUI 자동화 도구가 기존의 다른 Blender 창을 선택하여 수동 경로 검증을 진행하지 않았습니다. 별도 프로세스 및 취소 함수는 실제 Blender 안에서 검사했습니다.
- ZIP의 사용자 프로필 설치, GitHub Actions, 원격 Extension 설치·업데이트는 수행하지 않았습니다.
- 폴리곤 감소·강제 대칭·적응형 밀도·해부학적 엣지 루프는 미구현입니다. 대형 스캔 입력과 변형용 메시 품질은 검증하지 않았습니다.
