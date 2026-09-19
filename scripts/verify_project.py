from __future__ import annotations

import ast
import py_compile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = "zzamjak_3d_remesher"
REQUIRED_FILES = (
    "blender_manifest.toml",
    "__init__.py",
    "scripts/dev_run.sh",
    "scripts/dev_run.ps1",
    "scripts/dev_run.bat",
    "scripts/dev_bootstrap.py",
)


def main() -> int:
    for relative in REQUIRED_FILES:
        path = ROOT / relative
        if not path.exists():
            raise AssertionError(f"필수 파일이 없습니다: {relative}")

    manifest = tomllib.loads((ROOT / "blender_manifest.toml").read_text(encoding="utf-8"))
    assert manifest["id"] == ADDON_ID
    assert manifest["version"] == "0.1.0"
    assert manifest["type"] == "add-on"
    assert "SPDX:GPL-3.0-or-later" in manifest["license"]

    shell_text = (ROOT / "scripts/dev_run.sh").read_text(encoding="utf-8")
    ps_text = (ROOT / "scripts/dev_run.ps1").read_text(encoding="utf-8-sig")
    bat_text = (ROOT / "scripts/dev_run.bat").read_text(encoding="ascii")
    assert ADDON_ID in shell_text
    assert ADDON_ID in ps_text
    assert "dev_run.ps1" in bat_text
    assert "BLENDER_USER_RESOURCES" in shell_text
    assert "BLENDER_USER_RESOURCES" in ps_text

    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        py_compile.compile(path, doraise=True)
        ast.parse(path.read_text(encoding="utf-8"))

    print("프로젝트 정적 검증 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
