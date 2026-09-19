"""게시된 설치 ZIP으로 Blender 공식 원격 저장소를 생성한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tomllib
import zipfile
from pathlib import Path

from build_extension import PROJECT_ID, blender_binary_default, run_blender_extension


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--blender-binary", default=blender_binary_default())
    args = parser.parse_args()
    source = args.release_dir.resolve()
    output = args.output_dir.resolve()
    packages = sorted(source.glob(f"{PROJECT_ID}-v*.zip"))
    if not packages:
        raise ValueError("게시할 릴리스 ZIP이 없습니다.")
    if output.exists() and any(output.iterdir()):
        raise ValueError("저장소 출력 폴더는 비어 있어야 합니다.")
    output.mkdir(parents=True, exist_ok=True)
    selected = {}
    for package in packages:
        with zipfile.ZipFile(package) as archive:
            manifest = tomllib.loads(archive.read("blender_manifest.toml").decode("utf-8"))
        if manifest["id"] != PROJECT_ID or package.name != f"{PROJECT_ID}-v{manifest['version']}.zip":
            raise ValueError(f"릴리스 파일명과 manifest가 다릅니다: {package.name}")
        if not re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]):
            raise ValueError("업데이트 저장소에는 정식 숫자 버전만 게시합니다.")
        compatibility = (
            manifest["blender_version_min"],
            manifest.get("blender_version_max", ""),
            tuple(sorted(manifest.get("platforms", []))),
        )
        version = tuple(map(int, manifest["version"].split(".")))
        previous = selected.get(compatibility)
        if previous is None or version > previous[0]:
            selected[compatibility] = (version, package)
    packages = [package for _version, package in selected.values()]
    for package in packages:
        run_blender_extension(args.blender_binary, ["validate", str(package)], source)
        shutil.copy2(package, output / package.name)
    run_blender_extension(args.blender_binary, ["server-generate", "--repo-dir", str(output), "--html"], output)
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    if len(index["data"]) != len(packages):
        raise ValueError("릴리스 ZIP 수와 생성된 인덱스 항목 수가 다릅니다.")
    for item in index["data"]:
        name = item["archive_url"].removeprefix("./")
        if Path(name).name != name:
            raise ValueError("인덱스 자산은 같은 폴더의 ZIP이어야 합니다.")
        package = output / name
        expected_hash = "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest()
        if item["archive_hash"] != expected_hash or item["archive_size"] != package.stat().st_size:
            raise ValueError(f"인덱스 해시 또는 크기가 다릅니다: {name}")
    (output / ".nojekyll").touch()
    print(f"Extensions 저장소 검증 완료: {len(packages)}개 ZIP, {output / 'index.json'}")


if __name__ == "__main__":
    main()
