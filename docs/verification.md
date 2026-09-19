# 개발 기반 검증

2026-09-19, macOS의 실제 Blender 5.2.0 LTS에서 0.1.0 개발 기반을 검증했습니다. 이 문서는 구현 범위의 인수 결과이며 리토폴로지 품질 평가가 아닙니다.

## 통과한 검사

| 검사 | 결과 |
| --- | --- |
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | 18개 통과 |
| `python3 scripts/verify_project.py` | 파일·메타데이터·Python 정적 검사 통과 |
| `bash -n scripts/dev_run.sh` | 셸 구문 검사 통과 |
| `./scripts/dev_run.sh --background --python tests/blender_smoke.py` | 실제 Blender에서 통과 |
| `python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender` | 공식 소스 검증·빌드·ZIP 검증 통과 |
| 실행기의 `--python-expr`에 의도적인 예외 전달 | 종료 코드 1, 실패를 성공으로 숨기지 않음 |

Blender 검사에서는 전용 USER 리소스와 소스 링크, 애드온 활성화, manifest 및 등록 메타데이터 버전, 메시 분석, 가이드의 소스 로컬 좌표 변환, 밀도 속성 생성과 기존 값 보존, 속성 이름 충돌 거부, 편집 모드 분석 거부, 원본 지오메트리 보존 및 등록 해제를 확인했습니다. 오류 경로 검증 중 출력되는 빈 이름·속성 충돌·편집 모드 오류는 의도한 결과입니다.

개발 프로필은 `~/Library/Application Support/Blender/3DRemesherDev/5.2/`이며, 일상 프로필에 애드온을 설치하거나 설정을 저장하지 않았습니다.

## 설치용 산출물

- 파일: `dist/zzamjak_3d_remesher-v0.1.0.zip`
- 크기: 20,465 바이트
- SHA-256: `8a9f68acd823d308d18a3fd7f3c3f6855ad40e176be9dc0a6e795564805bc9ec`
- 포함 내용: manifest, GPL 전문, 루트 등록 모듈, `addon/*.py`
- 개발 스크립트·테스트·문서·CI·캐시 제외 및 ZIP 무결성 확인

해시는 이번 생성본의 값이며 재빌드 시 ZIP 메타데이터에 따라 달라질 수 있습니다.

## 아직 검증하지 않은 범위

- Windows 실제 실행: PowerShell 5.1용 UTF-8 BOM, ASCII 배치 파일, Junction의 기존 데이터 보호, 인자·종료 코드 전달을 정적으로 검토했습니다. 이 환경에는 PowerShell 파서가 없어 구문 실행 및 런타임 검증은 수행하지 않았습니다.
- Blender 4.2 실제 실행: 최소 버전 선언만 있으며 현재 실행 검증은 5.2.0입니다.
- GUI에서 패널의 시각적 배치와 수동 페인트 작업, ZIP의 사용자 프로필 설치는 검증하지 않았습니다.
- GitHub Actions 실행 및 원격 Extension 설치·업데이트: 구성만 준비했습니다. CI의 Blender 5.2.0 Linux 다운로드 주소는 HTTP 200 응답을 확인했습니다.
- 쿼드 생성·대칭 결과·엣지 루프 품질: 엔진 미구현으로 아직 평가 대상이 아닙니다.
