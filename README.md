# 3D Remesher

게임 모델용 독립 쿼드 리토폴로지 도구를 개발하기 위한 Blender Extension입니다. **0.2.0은 원본을 보존하고 새 쿼드 메시를 만드는 독립 Python 실험 엔진입니다.** 전문적인 변형용 엣지 루프를 자동 생성하는 단계까지 완성된 것은 아닙니다. Quad Remesher의 코드나 상용 엔진을 포함하지 않습니다.

## 현재 기능

| 항목 | 현재 상태 |
| --- | --- |
| Blender N 사이드바, 애드온 등록·해제 | 구현 |
| 메시 분석과 엔진 입력 추출 | 구현 |
| 쿼드 메시 생성 | 특징선 보존 패치 생성과 공유 엣지 분할 |
| 목표 쿼드 수 | 가까운 균일 분할 단계 선택, 실제 개수와 차이 표시 |
| 하드 엣지 | sharp/seam 표시와 면 사이 각도에 따라 패치 병합 제한 |
| 가이드 커브 | 인접 삼각형을 묶는 방향의 선호도에 반영 |
| 강제 X/Y/Z 대칭, 적응형 페인트 밀도 | 미지원 상태 표시, 입력 준비 기능 유지 |
| 폴리곤 감소, 전역 엣지 흐름 최적화 | 후속 엔진 개발 필요 |
| macOS / Windows 개발 실행기, ZIP 패키징, CI 구성 | 제공 |

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

전용 프로필은 `~/Library/Application Support/Blender/3DRemesherDev/<major.minor>/`입니다. `extensions/user_default/zzamjak_3d_remesher` 심링크가 현재 소스를 가리킵니다. 최초 활성화 시 전용 프로필에만 환경설정을 저장합니다.

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
3. 목표 쿼드 수와 하드 엣지 각도를 설정합니다. 목표 수는 정확한 결과 개수를 보장하지 않습니다.
4. 방향 힌트가 필요하면 Curve 오브젝트 이름에 `REMESH_GUIDE_` 접두사를 붙입니다. POLY와 BEZIER를 지원하며 NURBS는 오류로 알립니다. 가이드는 삼각형 병합 선호도에만 영향을 주며, 전역 엣지 루프를 강제하지 않습니다.
5. **메시 분석**으로 입력을 확인한 뒤 **리메시 실행**을 누릅니다.
6. 별도 결과 오브젝트에서 실제 쿼드 수와 품질, 적용되지 않은 설정 경고를 확인합니다. 원본은 남아 있으므로 비교할 때 원본을 잠시 숨길 수 있습니다.

GUI에서는 별도 Python 프로세스에서 계산하며 진행률을 표시합니다. 실행 중 `Esc`로 취소할 수 있습니다. 계산 중 원본 메시나 씬이 바뀌면 결과를 적용하지 않습니다.

이 버전은 기존 메시를 줄이는 저폴리 생성기가 아닙니다. 처음 생성된 쿼드 수보다 낮은 목표를 지정하면 감소하지 않고 차이를 경고합니다. 분할은 균일하게 적용하므로 목표와 실제 개수가 다를 수 있습니다. 전역 방향장, 해부학적 루프, 강제 대칭, 페인트에 따른 지역별 쿼드 수 조절은 후속 단계입니다.

**밀도 속성 준비**는 향후 엔진을 위한 데이터 준비 기능입니다. POINT/FLOAT_COLOR 속성 `remesh_density`의 기존 값은 보존하고 새 값은 1로 초기화합니다. 현재 쿼드 분포에는 적용되지 않습니다.

입력은 원본 메시 데이터이며 모디파이어가 적용된 평가 결과가 아닙니다. 출력에는 원본 UV, 스킨 웨이트, 셰이프 키를 전송하지 않습니다. 실패한 입력은 원본을 수정하지 않고 오류를 알립니다.

## 검증과 패키징

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/dev_run.sh --background --python tests/blender_smoke.py
./scripts/dev_run.sh --background --python tests/blender_engine.py
python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender
```

비교용 데모는 삼각형 80개의 구와 쿼드 240개의 결과를 나란히 저장합니다. 다음 명령으로 재생성하고 전용 개발 창에서 열 수 있습니다.

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
