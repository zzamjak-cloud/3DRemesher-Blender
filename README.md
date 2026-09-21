# 3D Remesher

게임 모델용 쿼드 리토폴로지를 목표로 개발 중인 Blender Extension입니다. **0.4.1은 평면 패치, 독립된 열린 관, 제한된 단일 구형 두부, 축에 정렬된 단일 T형 연결부에 새 쿼드 격자를 만듭니다. 불규칙하게 삼각화된 관의 단면 링, 돌출형 합성 얼굴의 눈·입·코·귀 가이드 폐루프 여섯 개, 엄격한 원형 단면의 합성 T형 연결부를 다룹니다. 그 밖의 일반 형상은 복셀 리메시와 QuadriFlow 로 새 쿼드 와이어를 깔고 원본 표면에 투영하는 범용 경로로 처리합니다. 이 범용 경로는 균일한 쿼드를 만들지만 얼굴·관절의 의도된 엣지 루프 배치는 보장하지 않으며, 실제 캐릭터의 변형 품질은 검증되지 않았습니다.** 원본을 보존하고 결과를 별도 오브젝트로 만듭니다.

핵심 생성 방식을 [루프·격자 중심의 새 엔진 설계](docs/topology-redesign.md)로 재설계했습니다. 아래 표는 0.4.1의 실제 범위를 나타냅니다.

## 현재 기능

| 항목 | 구현 방식 |
| --- | --- |
| 연속 격자 | 네 경계의 공평면 패치를 공유 분할 수로 연결합니다. 독립된 열린 삼각 관은 입력 삼각 연결과 별도로 단면 링과 길이 방향 열을 만듭니다. 새 단면 교차 경로는 원본 중간 정점이 같은 평면 링에 놓이지 않아도 두 평면 경계와 단일 별모양 단면을 가진 관을 처리합니다. 단일 구형 두부의 앞·양측에는 눈 둘·입·코·귀 둘의 바깥 `LOOP` 여섯 개를 배치하고, 제한된 합성 돌출형 얼굴에서도 경로를 확인합니다. 연결부는 사각 T와 축·원형 단면·가이드 배치가 엄격히 맞는 둥근 T에 한정됩니다. 실제 개구부와 일반 관절 형상은 미지원입니다. |
| 분리된 메시 조각 | 각 조각이 위 격자 생성기의 지원 형상일 때만 따로 처리합니다. 가이드는 가까운 단일 표면에 명확히 대응해야 합니다. 면적에 따라 목표 수를 나누고, 서로 접하는 조각의 정점을 병합하지 않습니다. 임모탈의 복잡한 다중 조각은 미지원입니다. |
| 범용 QuadriFlow 경로 | 격자 미지원 형상은 복셀 리메시(촘촘히) → 파편 제거 → Decimate → 매니폴드 수리 → 자식 Blender 프로세스의 QuadriFlow(시드·시간 제한 재시도) → 작은 구멍 쿼드 메움 → 원본 표면 투영 슈링크랩과 릴랙스로 처리합니다. 대칭은 양의 반쪽만 경계를 보존해 깔고 미러 용접합니다. 결과는 원본 표면 거리 4%·비다양체 0·종횡비 20·대칭 오차 검사를 거칩니다. 밀도 속성과 `DIRECTION` 가이드는 반영하지 않으며, 복셀 리메시가 좁은 틈과 내부 면을 닫을 수 있습니다. 경계 엣지 비율이 20%를 넘는 열린 판 형상은 부피가 없어 이 경로를 건너뜁니다. |
| 생성 방식 선택 | `자동`은 격자 → QuadriFlow → 실험 엔진 순서로 시도합니다. 밀도 속성이나 `DIRECTION` 가이드가 있으면 이를 반영하는 실험 엔진을 유지하고, 실험 엔진을 쓴 경우 경고합니다. `격자 전용`은 미지원 형상에서 중단합니다. `QuadriFlow`는 격자 탐색 없이 범용 경로만 실행합니다. `실험 엔진`은 기존 경로를 선택합니다. |
| 목표 쿼드 수·폴리곤 감소 | 적응형 삼각 메시 축소·분할, 결과 개수에 따른 예산 보정 |
| 대칭 X/Y/Z | 로컬 양의 축 영역 절단, 미러, 중앙 경계 정점 공유 |
| 특징선 | sharp/seam, 경계와 면 사이 각도를 보존 |
| 방향 정렬 | 특징선·곡률·가이드 4방향장, 패치 선택과 정점 정렬. 위에 열거한 제한된 형상 밖의 전역 연속 루프 배치는 미구현 |
| Guide Curves | 열린 기본 커브는 방향 힌트입니다. 지원되는 독립 관의 닫힌 `LOOP`와 양끝까지 이어지는 `STRIP`은 출력 링·엣지 열로 고정합니다. 단일 두부의 눈·입·코·귀 바깥 `LOOP` 여섯 개를 각각 폐경로로 만듭니다. 둥근 T에는 몸통 링 둘, 팔 링 하나, 팔 방향 `STRIP` 하나가 필요합니다. 다른 필수 경로를 보존할 수 없으면 중단합니다. |
| Vertex Paint 밀도 | `remesh_density` 값을 엣지 축소·분할 비용에 반영 |
| 원본 표면 | 내부 정점 재투영, 격자 경로의 양방향 표면 표본 편차 측정 |
| 큰 입력 처리 | 지원되는 격자 형상은 원본에서 직접 배치합니다. 그 외에는 QuadriFlow 경로가 원본을 직접 복셀 리메시하고, 그것도 실패하면 격리된 background Blender에서 임시 프록시를 만들고 자체 쿼드 엔진으로 처리합니다. |
| 작업 제어 | 별도 프로세스, 바이너리 입력 버퍼, 진행률, Esc 취소, 실패 시 원본 보존. 최대 쿼드 종횡비가 20을 넘는 결과는 적용하지 않습니다. |

목표 수는 제약 안에서 최대한 맞추지만 정확한 개수를 보장하지 않습니다. 얼굴의 여섯 루프는 바깥 윤곽만 만들고 내부를 쿼드로 채웁니다. 눈꺼풀·입술의 여러 겹 띠, 개구부, 자동 부위 인식은 포함하지 않습니다. 기존 임모탈 메시를 포함한 일반 캐릭터 전체에서의 성공은 검증하지 않았습니다. 실제 캐릭터의 루프와 변형 품질은 모델마다 검토해야 합니다.

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

GUI에서는 별도 프로세스로 계산하며 `Esc`로 취소할 수 있습니다. `자동`·`QuadriFlow` 방식은 bpy 가 필요해 background Blender 를 worker 로 띄우고, `격자 전용`·`실험 엔진`은 번들 Python 을 사용합니다. 입력이 계산 중 변경되면 결과 적용을 중단합니다. 밀도는 전체 목표 예산 안에서 지역 분포를 조절하며, 특징선과 경계 보존이 우선됩니다.

기존 엔진 한도보다 큰 입력도 격자 배치가 가능한 경우 원본에서 직접 처리합니다. 그 외에는 원본을 직접 바꾸지 않고 임시 프록시에서 처리합니다. worker가 격리된 background Blender를 열어 원본 경계와 sharp/seam 정점을 보호한 Decimate 프록시를 만든 뒤, 자체 쿼드 엔진을 실행하고 원본 BVH 기준 표면 편차·경계·명시 특징선 보존을 다시 검사합니다. 검사 실패나 취소가 발생하면 결과 오브젝트를 만들지 않습니다.

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
./scripts/dev_run.sh --background --python tests/blender_guided_surface_worker.py
./scripts/dev_run.sh --background --python tests/blender_face_patch_worker.py
./scripts/dev_run.sh --background --python tests/blender_triangulated_limb_worker.py
./scripts/dev_run.sh --background --python tests/blender_component_worker.py
./scripts/dev_run.sh --background --python tests/blender_irregular_tube_worker.py
./scripts/dev_run.sh --background --python tests/blender_anthropomorphic_face_worker.py
./scripts/dev_run.sh --background --python tests/blender_rounded_branch_worker.py
./scripts/dev_run.sh --background --python tests/blender_branch_t_worker.py
./scripts/dev_run.sh --background --python tests/blender_quadriflow_worker.py
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
