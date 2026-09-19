"""3D Remesher Blender Extension 빌드와 검증 도우미."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Iterable


PROJECT_ID = "zzamjak_3d_remesher"
PROJECT_NAME = "3D Remesher"
DEFAULT_BLENDER_MAC = "/Applications/Blender.app/Contents/MacOS/Blender"
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".omc",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "scripts",
    "tests",
    "wiki",
}
ALLOWED_RUNTIME_DIRS = {
    "addon",
    "assets",
    "icons",
    "operators",
    "panels",
    "presets",
    "properties",
    "resources",
    "vendor",
}
ALLOWED_ROOT_FILES = {"LICENSE"}
ALLOWED_RUNTIME_SUFFIXES = {".py", ".json", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".glsl"}


def load_manifest(source_dir: Path) -> dict:
    manifest_path = source_dir / "blender_manifest.toml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"blender_manifest.toml을 찾을 수 없습니다: {manifest_path}")
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("id") != PROJECT_ID:
        raise ValueError(f"manifest id가 {PROJECT_ID!r}가 아닙니다: {data.get('id')!r}")
    if data.get("name") != PROJECT_NAME:
        raise ValueError(f"manifest name이 {PROJECT_NAME!r}가 아닙니다: {data.get('name')!r}")
    if not data.get("version"):
        raise ValueError("manifest version이 필요합니다.")
    return data


def _is_forbidden(path: Path) -> bool:
    if path.name.startswith(".") and path.name not in {".keep"}:
        return True
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return True
    if path.suffix == ".zip":
        return True
    if path.suffix.startswith(".blend") and path.suffix != ".blend":
        return True
    return False


def _assert_relative(path_text: str) -> Path:
    rel = Path(path_text)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"빌드 경로는 소스 내부 상대 경로여야 합니다: {path_text}")
    if "\\" in path_text:
        raise ValueError(f"빌드 경로는 / 구분자를 사용해야 합니다: {path_text}")
    return rel


def _iter_declared_build_paths(source_dir: Path, manifest: dict) -> list[Path] | None:
    build = manifest.get("build") or {}
    paths = build.get("paths")
    if paths is None:
        return None
    if not isinstance(paths, list) or not all(isinstance(item, str) and item for item in paths):
        raise ValueError("[build].paths는 비어 있지 않은 문자열 목록이어야 합니다.")

    result: list[Path] = []
    for path_text in paths:
        rel = _assert_relative(path_text)
        if rel.name == "blender_manifest.toml":
            raise ValueError("[build].paths에는 manifest를 직접 넣지 않습니다.")
        if _is_forbidden(rel):
            raise ValueError(f"런타임 패키지에 넣을 수 없는 경로입니다: {path_text}")
        absolute = source_dir / rel
        if not absolute.exists():
            raise FileNotFoundError(f"[build].paths 경로가 없습니다: {path_text}")
        result.append(rel)
    return result


def _iter_runtime_candidates(source_dir: Path) -> Iterable[Path]:
    for path in source_dir.rglob("*"):
        rel = path.relative_to(source_dir)
        if path.is_dir() or _is_forbidden(rel):
            continue
        if path.name == "blender_manifest.toml":
            continue
        if len(rel.parts) == 1 and path.name in ALLOWED_ROOT_FILES:
            yield rel
            continue
        if len(rel.parts) == 1 and path.suffix == ".py":
            yield rel
            continue
        if rel.parts and rel.parts[0] in ALLOWED_RUNTIME_DIRS and path.suffix.lower() in ALLOWED_RUNTIME_SUFFIXES:
            yield rel


def select_runtime_paths(source_dir: Path, manifest: dict) -> list[Path]:
    declared = _iter_declared_build_paths(source_dir, manifest)
    if declared is not None:
        paths = declared
    else:
        paths = list(_iter_runtime_candidates(source_dir))

    wheels = manifest.get("wheels") or []
    if not isinstance(wheels, list):
        raise ValueError("manifest wheels는 목록이어야 합니다.")
    for wheel in wheels:
        if not isinstance(wheel, str):
            raise ValueError("manifest wheels 항목은 문자열이어야 합니다.")
        rel = _assert_relative(wheel)
        if _is_forbidden(rel) or rel.suffix != ".whl":
            raise ValueError(f"허용되지 않는 wheel 경로입니다: {wheel}")
        paths.append(rel)

    unique = sorted({path.as_posix(): path for path in paths}.values(), key=lambda item: item.as_posix())
    if Path("__init__.py") not in unique:
        raise FileNotFoundError("런타임 패키지에 __init__.py가 필요합니다.")
    return unique


def stage_runtime_source(source_dir: Path, stage_dir: Path, runtime_paths: Iterable[Path]) -> None:
    source_dir = source_dir.resolve()
    stage_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_dir / "blender_manifest.toml", stage_dir / "blender_manifest.toml")

    for rel in runtime_paths:
        src = (source_dir / rel).resolve()
        try:
            src.relative_to(source_dir)
        except ValueError as exc:
            raise ValueError(f"패키지 경로가 소스 밖을 가리킵니다: {rel}") from exc
        if src.is_dir():
            raise ValueError(f"패키지 경로는 파일이어야 합니다: {rel}")
        dst = stage_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def blender_binary_default() -> str:
    for key in ("REMESHER_BLENDER_BINARY", "BLENDER_BINARY"):
        value = os.environ.get(key)
        if value:
            return value
    return DEFAULT_BLENDER_MAC if Path(DEFAULT_BLENDER_MAC).exists() else "blender"


def run_blender_extension(blender_binary: str, args: list[str], cwd: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="3dremesher-blender-profile-") as profile:
        profile_path = Path(profile)
        env = os.environ.copy()
        env.update(
            {
                "BLENDER_USER_RESOURCES": str(profile_path),
                "BLENDER_USER_CONFIG": str(profile_path / "config"),
                "BLENDER_USER_SCRIPTS": str(profile_path / "scripts"),
                "BLENDER_USER_DATAFILES": str(profile_path / "datafiles"),
                "BLENDER_USER_EXTENSIONS": str(profile_path / "extensions"),
            }
        )
        command = [
            blender_binary,
            "--background",
            "--factory-startup",
            "--command",
            "extension",
            *args,
        ]
        subprocess.run(command, cwd=str(cwd), env=env, check=True)


def build(args: argparse.Namespace) -> Path:
    source_dir = args.source_dir.resolve()
    manifest = load_manifest(source_dir)
    runtime_paths = select_runtime_paths(source_dir, manifest)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{manifest['id']}-v{manifest['version']}.zip"

    run_blender_extension(args.blender_binary, ["validate", str(source_dir)], source_dir)

    with tempfile.TemporaryDirectory(prefix="3dremesher-extension-src-") as temp_dir:
        stage_dir = Path(temp_dir) / manifest["id"]
        stage_runtime_source(source_dir, stage_dir, runtime_paths)
        run_blender_extension(
            args.blender_binary,
            [
                "build",
                "--source-dir",
                str(stage_dir),
                "--output-filepath",
                str(output_path),
            ],
            source_dir,
        )

    run_blender_extension(args.blender_binary, ["validate", str(output_path)], source_dir)
    return output_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="3D Remesher Extension을 빌드하고 검증합니다.")
    parser.add_argument("--source-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--blender-binary", default=blender_binary_default())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        output_path = build(args)
    except Exception as exc:
        print(f"빌드 실패: {exc}", file=sys.stderr)
        return 1

    print(f"빌드 완료: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
