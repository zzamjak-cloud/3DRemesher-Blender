# 3D Remesher

게임 모델용 독립 쿼드 리토폴로지 Blender Extension입니다. **0.3.0은 메시 감소, 목표 수 제어, X/Y/Z 대칭, 가이드 방향장과 페인트 밀도를 처리합니다.** 원본을 보존하고 결과를 별도 오브젝트로 만듭니다. Quad Remesher나 Blender 내장 Remesh 엔진을 호출하지 않습니다.

## 현재 기능

| 항목 | 구현 방식 |
| --- | --- |
| 목표 쿼드 수·폴리곤 감소 | 적응형 삼각 메시 축소·분할, 결과 개수에 따른 예산 보정 |
| 대칭 X/Y/Z | 로컬 양의 축 영역 절단, 미러, 중앙 경계 정점 공유 |
| 특징선 | sharp/seam, 경계와 면 사이 각도를 보존 |
| 엣지 흐름 | 특징선·곡률·가이드 4방향장, 패치 선택과 정점 정렬 |
| Guide Curves | POLY·BEZIER 방향을 주변 표면에 전파 |
| Vertex Paint 밀도 | `remesh_density` 값을 엣지 축소·분할 비용에 반영 |
| 원본 표면 | 내부 정점 재투영과 표면 편차 측정 |
| 작업 제어 | 별도 프로세스, 진행률, Esc 취소, 실패 시 원본 보존 |

실제 캐릭터의 눈·입·관절에 필요한 루프와 변형 품질은 모델마다 검토해야 합니다. 이 버전의 기능 구현을 상용 도구와 동등한 품질 보증으로 해석하지 않습니다.

Blender 최소 버전은 4.2이며, 실제 로컬 실행은 5.2.0 LTS에서 검증했습니다. 개발 스크립트와 단위 테스트에는 Python 3.11 이상이 필요합니다. Windows 및 Blender 4.2 런타임은 아직 검증하지 않았습니다.

## macOS 개발 실행

프로젝트 루트에서 실행합니다.

```bash
./scripts/dev_run.sh
./scripts/dev_run.sh --background --python tests/blender_smoke.py
```

기본 실행 파일은 `/Applications/Blender.app/Contents/MacOS/Blender`입니다. 다른 설치본은 `BLENDER_BINARY` 또는 `REMESHER_BLENDER_BINARY`로 지정합니다.

```bash
BLENDER_BINARY="/다른/경로/Blender" ./scripts/dev_run.sh
```

전용 프로필은 `~/Library/Application Support/Blender/3DRemesherDev/<major.minor>/`입니다. `extensions/user_default/zzamjak_3d_remesher` 심링크가 현재 소스를 가리킵니다. 최초 활성화 시 전용 프로필에만 환경설정을 저장합니다. 임시 파일과 종료 복구 파일도 전용 프로필의 `temp/`로 분리합니다.

다른 개발 프로필 위치가 필요하면 `REMESHER_DEV_PROFILE_ROOT`로 전용 상위 폴더를 지정할 수 있습니다. 실행기가 그 아래에 `<major.minor>` 폴더를 만듭니다. Windows 실행기도 같은 환경 변수를 지원합니다.

## Windows 개발 실행

PowerShell에서 실행 파일을 지정합니다.

```powershell
$env:REMESHER_BLENDER_BINARY = 'C:/Program Files/Blender Foundation/Blender 4.2/blender.exe'
./scripts/dev_run.ps1
./scripts/dev_run.ps1 -Background -BlenderArgs @('--python', 'tests/blender_smoke.py')
```

명령 프롬프트에서는 `scripts/dev_run.bat`을 사용합니다. 기본 검색 위치는 프로젝트의 `Blender/blender.exe`, `tools/Blender/blender.exe`, 이후 PATH입니다. 프로필은 `%LOCALAPPDATA%/3DRemesher-Blender/Blender/<major.minor>/`이며, 전용 Extension 경로의 Junction에서 소스를 직접 로드합니다.

## Blender 사용 순서

1. 개발 실행기로 Blender를 열고 오브젝트 모드에서 메시를 선택합니다.
2. 3D 뷰포트의 `N` 사이드바에서 **3D Remesher**를 엽니다.
3. 목표 쿼드 수와 하드 엣지 각도를 설정합니다. 위상·특징선·대칭과 형상 보존 제약으로 실제 개수가 달라질 수 있습니다. 표면 편차가 크면 해상도를 높여 재시도하고, 기준을 충족하지 못한 결과는 적용하지 않습니다. 실제 개수와 오차율을 보고합니다.
4. 대칭 축은 **오브젝트 로컬 원점**을 기준으로 합니다. 선택한 모든 축의 양의 영역을 유지하고 반대편을 만듭니다. 음의 영역에만 있는 입력은 거부합니다.
5. 방향 힌트가 필요하면 Curve 이름에 `REMESH_GUIDE_` 접두사를 붙입니다. POLY와 BEZIER를 지원하며 NURBS는 오류로 알립니다. 가이드는 방향 제약이며 정점을 곡선 위에 고정하는 기능은 아닙니다.
6. **밀도 속성 준비**로 POINT/FLOAT_COLOR `remesh_density`를 만들고 Vertex Paint로 값을 칠합니다. 밝은 영역에 더 많은 쿼드를 배정합니다. 속성이 없으면 균일 밀도를 사용합니다. 밀도 대비 설정으로 영향력을 조절합니다.
7. **메시 분석 → 리메시 실행** 후 실제 쿼드 수, 표면 편차, 대칭 오차와 방향 정렬 점수를 확인합니다. 원본은 남아 있으므로 비교할 때 잠시 숨길 수 있습니다.

GUI에서는 별도 Python 프로세스로 계산하며 `Esc`로 취소할 수 있습니다. 입력이 계산 중 변경되면 결과 적용을 중단합니다. 밀도는 전체 목표 예산 안에서 지역 분포를 조절하며, 특징선과 경계 보존이 우선됩니다.

입력은 모디파이어가 적용되기 전 원본 메시입니다. UV, 스킨 웨이트, 셰이프 키는 결과로 전송하지 않습니다. 리토폴로지 결과를 게임에 사용하기 전에 베이크와 리깅 전송이 별도로 필요합니다.

## 검증과 패키징

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/dev_run.sh --background --python tests/blender_smoke.py
./scripts/dev_run.sh --background --python tests/blender_engine.py
./scripts/dev_run.sh --background --python tests/blender_acceptance.py
./scripts/dev_run.sh --python tests/blender_modal.py
python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender
```

비교용 데모는 원본 구와 목표 수에 맞춰 생성한 쿼드 결과를 나란히 저장합니다. 다음 명령으로 재생성하고 전용 개발 창에서 열 수 있습니다.

```bash
./scripts/dev_run.sh --background --python tests/blender_demo.py
./scripts/dev_run.sh dist/3DRemesher_Prototype.blend
```

설치용 ZIP은 `dist/`에 생성됩니다. Blender 환경설정의 **Get Extensions → Install from Disk**에서 설치할 수 있습니다. 개발 중에는 ZIP 재설치 대신 위 소스 연결 실행기를 사용합니다.

- [엔진 입력 계약과 후속 개발 순서](docs/architecture.md)
- [실제 검증 결과와 한계](docs/verification.md)
- [변경 이력](CHANGELOG.md)

## 배포 상태

현재 사용자용 원격 Extension 저장소 URL은 없습니다. 공개 저장소·Release·Pages는 게시하지 않았고, CI도 로컬 구성만 준비했습니다. 추후 배포 후보는 `zzamjak-cloud/3DRemesher-Blender`와 `https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json`이며 **현재 설치용 주소가 아닙니다**.

배포 후에는 확정된 URL을 **Get Extensions → Repositories → Add Remote Repository**에 등록합니다. **Check for Updates on Startup**은 새 버전을 확인하고 알리는 기능이며, 업데이트 설치는 사용자가 승인합니다.

## 라이선스와 참고

프로젝트 코드는 GPL-3.0-or-later입니다. [LICENSE](LICENSE)에 전문이 있습니다. 외부 엔진을 도입할 때 해당 엔진과 의존성의 라이선스를 별도로 확인해야 합니다.

- [Blender Extension 제작](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html)
- [Blender Extension CLI](https://docs.blender.org/manual/en/4.2/advanced/command_line/extension_arguments.html)
