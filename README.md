# 3D Remesher

게임 모델용 독립 쿼드 리토폴로지 도구를 개발하기 위한 Blender Extension입니다. **0.1.0은 프로젝트 개발 기반이며, 쿼드 메시 생성 엔진은 아직 구현되지 않았습니다.** Quad Remesher의 코드나 상용 엔진을 포함하지 않습니다.

## 현재 기능

| 항목 | 현재 상태 |
| --- | --- |
| Blender N 사이드바, 애드온 등록·해제 | 구현 |
| 메시 분석과 엔진 입력 추출 | 구현 |
| 목표 쿼드 수, X/Y/Z 대칭, 하드 엣지 각도 | 입력 설정 준비 |
| 가이드 커브와 페인트 밀도 | 제어점 수집 및 밀도 속성 준비 |
| 곡률 기반 엣지 흐름, 쿼드 메시 생성 | 후속 엔진 개발 필요 |
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
3. 목표 쿼드 수, 대칭 축, 하드 엣지 각도와 밀도 속성 이름을 설정합니다.
4. **밀도 속성 준비**로 기본 `remesh_density` 속성을 생성합니다. 이 속성은 POINT 도메인의 FLOAT_COLOR이며 붉은 채널을 밀도로 읽습니다. 페인트할 때 해당 컬러 속성을 활성 속성으로 선택하세요. 기존 값은 보존하며 새 값은 1로 초기화합니다.
5. 가이드용 Curve 오브젝트 이름에 `REMESH_GUIDE_` 접두사를 붙입니다. 현재는 스플라인별 제어점만 수집합니다.
6. **메시 분석**으로 쿼드 비율, 경계 엣지, 비다양체 엣지와 퇴화 면을 확인합니다.

**리메시 실행**은 현재 입력 검증 후 엔진 미구현 안내와 함께 중단됩니다. 원본 지오메트리를 변경하거나 결과 메시를 만들지 않습니다. 밀도 속성 준비는 의도적으로 선택 메시의 컬러 속성을 추가하며 실행 취소를 지원합니다.

## 검증과 패키징

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/dev_run.sh --background --python tests/blender_smoke.py
python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender
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
