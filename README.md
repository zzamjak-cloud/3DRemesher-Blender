# 3D Remesher

게임 모델용 쿼드 리토폴로지를 목표로 개발 중인 Blender Extension입니다. **공개 0.3.0은 실험 엔진입니다. 로컬 개발본에는 큐브형 평면 패치와 열린 튜브의 격자 생성 경로가 추가됐지만, 캐릭터의 눈·입·관절 및 몸통 연결부의 루프 요구는 아직 충족하지 못합니다.** 원본을 보존하고 결과를 별도 오브젝트로 만듭니다. 현재 로컬 코드의 큰 입력 경로는 미지원 형상에 임시 Blender Decimate 프록시를 사용합니다.

핵심 생성 방식을 [루프·격자 중심의 새 엔진 설계](docs/topology-redesign.md)로 재설계했습니다. 아래 표는 로컬 개발본의 실제 범위를 나타냅니다.

## 현재 기능

| 항목 | 구현 방식 |
| --- | --- |
| 연속 격자 | 네 경계의 공평면 패치를 공유 분할 수로 연결합니다. 열린 튜브는 닫힌 단면 링과 길이 방향 열을 생성하며, 굽거나 가늘어지는 독립 튜브도 구조 검사를 통과했습니다. 연결부·얼굴은 미지원입니다. |
| 생성 방식 선택 | `자동`은 격자를 우선 사용하고 미지원 형상에는 실험 엔진으로 처리한 사실을 경고합니다. `격자 전용`은 미지원 형상에서 중단합니다. `실험 엔진`은 기존 경로를 선택합니다. |
| 목표 쿼드 수·폴리곤 감소 | 적응형 삼각 메시 축소·분할, 결과 개수에 따른 예산 보정 |
| 대칭 X/Y/Z | 로컬 양의 축 영역 절단, 미러, 중앙 경계 정점 공유 |
| 특징선 | sharp/seam, 경계와 면 사이 각도를 보존 |
| 방향 정렬 | 특징선·곡률·가이드 4방향장, 패치 선택과 정점 정렬. 연속 루프 설계는 미구현 |
| Guide Curves | 열린 기본 커브는 방향 힌트입니다. 원통 단면의 닫힌 `LOOP`와 양끝까지 이어지는 길이 방향 `STRIP`은 출력 링·엣지 열로 고정합니다. 다른 필수 경로를 보존할 수 없으면 중단합니다. |
| Vertex Paint 밀도 | `remesh_density` 값을 엣지 축소·분할 비용에 반영 |
| 원본 표면 | 내부 정점 재투영과 표면 편차 측정 |
| 큰 입력 처리 | 기존 엔진 한도를 넘는 경우에만 격리된 background Blender에서 임시 프록시를 만들고 자체 쿼드 엔진으로 처리 |
| 작업 제어 | 별도 프로세스, 진행률, Esc 취소, 실패 시 원본 보존 |

목표 수는 제약 안에서 최대한 맞추지만 정확한 개수를 보장하지 않습니다. 실제 캐릭터의 눈·입·관절에 필요한 루프와 변형 품질은 모델마다 검토해야 합니다. 이 버전의 기능 구현을 상용 도구와 동등한 품질 보증으로 해석하지 않습니다.

Blender 최소 버전은 4.2입니다. macOS의 5.2.0 LTS와 Linux CI의 4.2.0·5.2.0에서 실행을 검증했습니다. 개발 스크립트와 단위 테스트에는 Python 3.11 이상이 필요합니다. Windows 런타임은 아직 검증하지 않았습니다.

## 설치와 자동 업데이트

1. Blender 환경설정에서 **Get Extensions**를 열고 온라인 접근을 허용합니다.
2. **Repositories → Add Remote Repository**에 다음 주소를 등록합니다.

   ```text
   https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json
   ```

3. 저장소의 **Check for Updates on Startup**을 켭니다.
4. **3D Remesher**를 검색해 설치하고 활성화합니다.

시작 시 새 버전을 자동 확인하고 알립니다. 업데이트 설치는 사용자가 승인합니다. 즉시 확인하려면 **Check for Updates**를 사용합니다. [Blender 공식 안내](https://docs.blender.org/manual/en/5.1/editors/preferences/extensions.html)를 참고하세요.

[릴리스 ZIP](https://github.com/zzamjak-cloud/3DRemesher-Blender/releases/latest)을 **Install from Disk**로 설치할 수도 있습니다. 자동 업데이트를 받으려면 위 원격 저장소를 통해 설치하세요. 아래 개발 프로필은 개발자를 위한 별도 환경입니다.

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

1. 애드온을 설치한 Blender를 열고 오브젝트 모드에서 메시를 선택합니다.
2. 3D 뷰포트의 `N` 사이드바에서 **3D Remesher**를 엽니다.
3. 토폴로지 생성 방식과 목표 쿼드 수, 하드 엣지 각도를 설정합니다. 격자 전용은 지원 범위 밖에서 결과를 만들지 않습니다. 위상·특징선·대칭과 형상 보존 제약으로 실제 개수가 달라질 수 있습니다. 표면 편차가 크면 기준을 충족하지 못한 결과는 적용하지 않습니다. 실제 개수와 오차율을 보고합니다.
4. 대칭 축은 **오브젝트 로컬 원점**을 기준으로 합니다. 선택한 모든 축의 양의 영역을 유지하고 반대편을 만듭니다. 음의 영역에만 있는 입력은 거부합니다.
5. 방향 힌트가 필요하면 Curve 이름에 `REMESH_GUIDE_` 접두사를 붙입니다. POLY와 BEZIER를 지원하며 NURBS는 오류로 알립니다. 기본 열린 커브는 `DIRECTION`입니다. 닫힌 커브는 `LOOP`, 이름이 `REMESH_GUIDE_STRIP_`으로 시작하는 열린 커브는 `STRIP`으로 수집합니다. 필수 경로를 만들 수 없는 경우에는 결과를 생성하지 않습니다.
6. **밀도 속성 준비**로 POINT/FLOAT_COLOR `remesh_density`를 만들고 Vertex Paint로 값을 칠합니다. 밝은 영역에 더 많은 쿼드를 배정합니다. 속성이 없으면 균일 밀도를 사용합니다. 밀도 대비 설정으로 영향력을 조절합니다.
7. **메시 분석 → 리메시 실행** 후 실제 쿼드 수, 표면 편차, 대칭 오차와 방향 정렬 점수를 확인합니다. 원본은 남아 있으므로 비교할 때 잠시 숨길 수 있습니다.

GUI에서는 별도 Python 프로세스로 계산하며 `Esc`로 취소할 수 있습니다. 입력이 계산 중 변경되면 결과 적용을 중단합니다. 밀도는 전체 목표 예산 안에서 지역 분포를 조절하며, 특징선과 경계 보존이 우선됩니다.

기존 엔진 한도보다 큰 입력은 원본을 직접 바꾸지 않고 임시 프록시에서 처리합니다. worker가 격리된 background Blender를 열어 원본 경계와 sharp/seam 정점을 보호한 Decimate 프록시를 만든 뒤, 자체 쿼드 엔진을 실행하고 원본 BVH 기준 표면 편차·경계·명시 특징선 보존을 다시 검사합니다. 검사 실패나 취소가 발생하면 결과 오브젝트를 만들지 않습니다.

입력은 모디파이어가 적용되기 전 원본 메시입니다. UV, 스킨 웨이트, 셰이프 키는 결과로 전송하지 않습니다. 리토폴로지 결과를 게임에 사용하기 전에 베이크와 리깅 전송이 별도로 필요합니다.

## 검증과 패키징

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
./scripts/dev_run.sh --background --python tests/blender_smoke.py
./scripts/dev_run.sh --background --python tests/blender_engine.py
./scripts/dev_run.sh --background --python tests/blender_acceptance.py
./scripts/dev_run.sh --background --python tests/blender_structured_grid.py
./scripts/dev_run.sh --background --python tests/blender_structured_large.py
./scripts/dev_run.sh --background --python tests/blender_structured_worker.py
./scripts/dev_run.sh --background --python tests/blender_guided_worker.py
./scripts/dev_run.sh --python tests/blender_modal.py
python3 scripts/build_extension.py --blender-binary /Applications/Blender.app/Contents/MacOS/Blender
```

비교용 데모는 원본 구와 목표 수에 맞춰 생성한 쿼드 결과를 나란히 저장합니다. 다음 명령으로 재생성하고 전용 개발 창에서 열 수 있습니다.

```bash
./scripts/dev_run.sh --background --python tests/blender_demo.py
./scripts/dev_run.sh dist/3DRemesher_Prototype.blend
```

설치용 ZIP은 `dist/`에 생성됩니다. Blender 환경설정의 **Get Extensions → Install from Disk**에서 설치할 수 있습니다. 개발 중에는 ZIP 재설치 대신 위 소스 연결 실행기를 사용합니다.

- [새 엔진 설계와 품질 합격 기준](docs/topology-redesign.md)
- [기존 실험 엔진 구조](docs/architecture.md)
- [실제 검증 결과와 한계](docs/verification.md)
- [변경 이력](CHANGELOG.md)

## 릴리스 운영

`main`과 Pull Request에서 CI가 실행됩니다. 버전을 맞춘 새 `v<버전>` 태그를 게시하면 **Release**가 단위 테스트·실제 Blender 통합 검사·공식 ZIP 검증을 실행한 뒤 해당 ZIP과 `SHA256SUMS`를 GitHub Release에 첨부합니다. 기존 태그와 릴리스 자산은 덮어쓰지 않습니다.

릴리스 성공 후 **Extensions Repository**가 게시된 정식 릴리스 ZIP을 모아 동일한 Blender·플랫폼 호환 범위의 최신 버전을 선택합니다. Blender의 `extension server-generate`로 인덱스를 만들고 GitHub Pages에 배포합니다. 인덱스의 ZIP 크기와 SHA-256도 검사합니다. 저장소 갱신만 다시 실행할 때는 이 워크플로의 수동 실행을 사용합니다.

- [공개 소스](https://github.com/zzamjak-cloud/3DRemesher-Blender)
- [설치·업데이트 저장소](https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json)
- [배포 인수 검증 방법](docs/distribution.md)

## 라이선스와 참고

프로젝트 코드는 GPL-3.0-or-later입니다. [LICENSE](LICENSE)에 전문이 있습니다. 외부 엔진을 도입할 때 해당 엔진과 의존성의 라이선스를 별도로 확인해야 합니다.

- [Blender Extension 제작](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html)
- [Blender Extension CLI](https://docs.blender.org/manual/en/4.2/advanced/command_line/extension_arguments.html)
