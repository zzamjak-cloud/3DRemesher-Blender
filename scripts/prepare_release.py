"""태그와 검증된 ZIP을 대조하고 릴리스 설명 및 해시를 작성한다."""

from __future__ import annotations

import hashlib
import os
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "blender_manifest.toml").read_text(encoding="utf-8"))
    version = manifest["version"]
    if os.environ["RELEASE_TAG"] != f"v{version}":
        raise ValueError("릴리스 태그와 manifest 버전이 다릅니다.")
    dist = root / "dist"
    package = dist / f"{manifest['id']}-v{version}.zip"
    with zipfile.ZipFile(package) as archive:
        if archive.testzip() is not None:
            raise ValueError("설치 ZIP이 손상되었습니다.")
        bundled = tomllib.loads(archive.read("blender_manifest.toml").decode("utf-8"))
        if bundled != manifest:
            raise ValueError("설치 ZIP의 manifest가 소스와 다릅니다.")
        for name in archive.namelist():
            if archive.read(name) != (root / name).read_bytes():
                raise ValueError(f"검증된 설치 ZIP과 태그 소스가 다릅니다: {name}")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (dist / "SHA256SUMS").write_text(f"{digest}  {package.name}\n", encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    section = next(section for section in changelog.split("\n## ") if section.startswith(version + " —"))
    notes = (
        f"## {section.strip()}\n\n"
        "### 설치 및 업데이트\n\n"
        "Blender의 **Get Extensions → Repositories → Add Remote Repository**에 다음 주소를 등록합니다.\n\n"
        "https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json\n\n"
        "**Check for Updates on Startup**을 켜면 새 버전을 확인하고 알립니다. 설치는 사용자가 승인합니다.\n\n"
        "### 현재 한계\n\n"
        "형상 보존 제약으로 목표 쿼드 수와 실제 개수가 크게 다를 수 있습니다. "
        "캐릭터의 눈·입·관절 루프 및 변형 품질은 별도 검토가 필요합니다. "
        "UV·웨이트·셰이프 키 전송과 NURBS 가이드는 지원하지 않습니다. "
        "Windows와 Blender 4.2 런타임은 아직 검증하지 않았습니다.\n"
    )
    (dist / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"릴리스 입력 검증 완료: {package.name}, sha256:{digest}")


if __name__ == "__main__":
    main()
