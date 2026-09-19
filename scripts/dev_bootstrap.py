"""Blender 개발 프로필에서 3D Remesher 확장을 활성화한다."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import bpy


EXTENSION_ID = os.environ.get("REMESHER_EXTENSION_ID", "zzamjak_3d_remesher")
REPO_MODULE = os.environ.get("REMESHER_REPO_MODULE", "user_default")
REPO_NAME = os.environ.get("REMESHER_REPO_NAME", "3D Remesher Dev")
ROOT_ENV = "REMESHER_DEV_ROOT"
PROFILE_ENV = "REMESHER_DEV_PROFILE"


def _path_from_env(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} 환경 변수가 필요합니다.")
    return Path(value).expanduser().resolve()


def _require_inside(path: Path, parent: Path, label: str) -> None:
    path_abs = Path(os.path.abspath(path))
    parent_abs = Path(os.path.abspath(parent))
    try:
        path_abs.relative_to(parent_abs)
    except ValueError as exc:
        raise RuntimeError(f"{label} 경로가 전용 프로필 밖을 가리킵니다: {path}") from exc


def _ensure_repo(repo_dir: Path) -> bool:
    preferences = bpy.context.preferences
    expected_dir = str(repo_dir)

    for repo in preferences.extensions.repos:
        if repo.module == REPO_MODULE:
            changed = False
            if Path(repo.directory).expanduser().resolve() != repo_dir.resolve():
                repo.directory = expected_dir
                changed = True
            if repo.name != REPO_NAME:
                repo.name = REPO_NAME
                changed = True
            if not repo.enabled:
                repo.enabled = True
                changed = True
            return changed

    result = bpy.ops.extensions.repo_add(
        id=REPO_MODULE,
        name=REPO_NAME,
        directory=expected_dir,
        source="USER",
    )
    if "FINISHED" not in result:
        raise RuntimeError(f"Extension 저장소 등록 실패: {result}")
    return True


def _enable_extension(module_name: str) -> bool:
    import addon_utils

    if bpy.context.preferences.addons.get(module_name):
        return False

    try:
        addon_utils.enable(
            module_name,
            default_set=True,
            persistent=True,
            refresh_handled=False,
        )
    except TypeError:
        addon_utils.enable(module_name, default_set=True, persistent=True)

    if not bpy.context.preferences.addons.get(module_name):
        raise RuntimeError(f"확장 활성화 실패: {module_name}")
    return True


def main() -> None:
    repo_root = _path_from_env(ROOT_ENV)
    profile_root = _path_from_env(PROFILE_ENV)
    user_resource = Path(bpy.utils.resource_path("USER")).expanduser().resolve()

    print(f"[3D Remesher 개발] 프로필={profile_root}")
    print(f"[3D Remesher 개발] Blender 사용자 리소스={user_resource}")
    print(f"[3D Remesher 개발] 저장소={repo_root}")

    if user_resource != profile_root:
        raise RuntimeError(
            "Blender USER resource가 개발 프로필과 다릅니다: "
            f"{user_resource} != {profile_root}"
        )

    manifest = repo_root / "blender_manifest.toml"
    if not manifest.is_file():
        raise RuntimeError(f"blender_manifest.toml을 찾을 수 없습니다: {manifest}")

    repo_dir = profile_root / "extensions" / REPO_MODULE
    link_path = repo_dir / EXTENSION_ID
    _require_inside(repo_dir, profile_root, "Extension 저장소")
    _require_inside(link_path, profile_root, "Extension 링크")

    if not link_path.exists():
        raise RuntimeError(f"개발 Extension 링크가 없습니다: {link_path}")
    if link_path.resolve() != repo_root:
        raise RuntimeError(f"개발 Extension 링크 대상 불일치: {link_path.resolve()} != {repo_root}")

    changed = _ensure_repo(repo_dir)
    module_name = f"bl_ext.{REPO_MODULE}.{EXTENSION_ID}"
    changed = _enable_extension(module_name) or changed
    importlib.import_module(module_name)

    if changed:
        bpy.ops.wm.save_userpref()
        print("[3D Remesher 개발] 전용 프로필 사용자 설정 저장 완료")

    print(f"[3D Remesher 개발] 활성화됨={module_name}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[3D Remesher 개발] 부트스트랩 실패: {exc}", file=sys.stderr)
        raise
