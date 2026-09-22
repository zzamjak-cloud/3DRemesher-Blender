# Extensions 배포

## 사용자 설치

Blender 환경설정 **Get Extensions → Repositories → Add Remote Repository**에 다음 URL을 등록합니다.

```text
https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json
```

온라인 접근과 저장소의 **Check for Updates on Startup**을 켜고 **3D Remesher**를 설치합니다. 시작 시 새 버전을 확인하고 알리며, 설치는 사용자가 승인합니다. 일상 프로필은 배포 검증 과정에서 변경하지 않습니다.

## 배포 순서

1. `blender_manifest.toml`, `__init__.py`, 변경 이력의 버전을 맞춥니다.
2. 로컬 검증 후 커밋을 `main`에 게시하고 CI 통과를 확인합니다.
3. 원격에 없는 `v<버전>` 태그를 게시합니다.
4. **Release**가 Blender 4.2.0·5.2.0 통합 검사 후 검증한 ZIP과 `SHA256SUMS`를 게시합니다.
5. **Extensions Repository**가 성공한 Release 실행을 받아 Pages를 갱신합니다. `workflow_run`을 사용하므로 GitHub 작업 토큰으로 생성된 Release도 연결됩니다.
6. Release 자산, Pages 인덱스, 인덱스에 연결된 ZIP의 크기·해시를 비교합니다.
7. 배포 인수 프로필에서 이전 버전 설치 → 원격 동기화 → 업데이트 제공 → 업데이트 → 재시작·활성화·리메시를 확인합니다.

같은 호환 범위에는 최신 정식 버전만 인덱스에 넣습니다. 이전 ZIP은 GitHub Release에 남습니다. 호환 범위를 바꾸는 릴리스는 범위가 겹치지 않도록 검토해야 합니다. 기존 태그·자산을 덮어쓰지 않으며 수정은 새 버전으로 배포합니다.

## 격리된 인수 검증

`scripts/verify_distribution.py`는 새 임시 프로필을 만들며 개발 소스 심링크를 사용하지 않습니다. `--previous-zip`에는 실제 이전 버전 ZIP을 지정합니다.

```bash
python3 scripts/verify_distribution.py \
  --previous-zip dist/zzamjak_3d_remesher-v0.4.2.zip \
  --expected-version 0.5.1
```

검증 결과는 `dist/distribution-verification.json`에 저장합니다. 원격 저장소에서 동기화한 버전과 실제 설치 버전·활성화 상태·리메시 결과를 검사합니다.
