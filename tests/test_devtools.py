from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_build_module():
    spec = importlib.util.spec_from_file_location("build_extension", ROOT / "scripts" / "build_extension.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DevRunScriptTests(unittest.TestCase):
    def _fake_blender(self, directory: Path, exit_code: int = 7) -> tuple[Path, Path]:
        capture = directory / "capture.json"
        fake = directory / "fake blender"
        fake.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [[ "${1:-}" == "--version" ]]; then',
                    "  python3 - <<'PY'",
                    "print('Blender 5.2.0 LTS')",
                    "for index in range(20000):",
                    "    print(f'extra version line {index}')",
                    "PY",
                    "  exit $?",
                    "fi",
                    f"python3 - <<'PY' {json.dumps(str(capture))} \"$@\"",
                    "import json, os, sys",
                    "payload = {",
                    "    'argv': sys.argv[2:],",
                    "    'env': {key: os.environ.get(key) for key in (",
                    "        'BLENDER_USER_RESOURCES',",
                    "        'BLENDER_USER_CONFIG',",
                    "        'BLENDER_USER_SCRIPTS',",
                    "        'BLENDER_USER_DATAFILES',",
                    "        'BLENDER_USER_EXTENSIONS',",
                    "        'REMESHER_DEV_ROOT',",
                    "        'REMESHER_DEV_PROFILE',",
                    "        'REMESHER_DEV_PROFILE_ROOT',",
                    "        'REMESHER_EXTENSION_ID',",
                    "    )},",
                    "}",
                    "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False))",
                    "PY",
                    f"exit {exit_code}",
                ]
            ),
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return fake, capture

    def test_macos_runner_isolates_profile_and_forwards_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            profile_parent = temp_path / "profile root with spaces"
            fake, capture = self._fake_blender(temp_path)
            env = os.environ.copy()
            env.update(
                {
                    "REMESHER_BLENDER_BINARY": str(fake),
                    "REMESHER_DEV_PROFILE_ROOT": str(profile_parent),
                }
            )

            result = subprocess.run(
                [
                    str(ROOT / "scripts" / "dev_run.sh"),
                    "--background",
                    "--python-expr",
                    "print('space arg')",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 7)
            payload = json.loads(capture.read_text(encoding="utf-8"))
            profile = profile_parent / "5.2"
            link = profile / "extensions/user_default/zzamjak_3d_remesher"
            self.assertEqual(link.resolve(), ROOT)
            self.assertEqual(payload["env"]["BLENDER_USER_RESOURCES"], str(profile))
            self.assertEqual(payload["env"]["BLENDER_USER_CONFIG"], str(profile / "config"))
            self.assertEqual(payload["env"]["BLENDER_USER_SCRIPTS"], str(profile / "scripts"))
            self.assertEqual(payload["env"]["BLENDER_USER_DATAFILES"], str(profile / "datafiles"))
            self.assertEqual(payload["env"]["BLENDER_USER_EXTENSIONS"], str(profile / "extensions"))
            self.assertEqual(payload["env"]["REMESHER_DEV_ROOT"], str(ROOT))
            self.assertEqual(payload["env"]["REMESHER_DEV_PROFILE"], str(profile))
            self.assertEqual(payload["env"]["REMESHER_DEV_PROFILE_ROOT"], str(profile_parent))
            self.assertEqual(payload["env"]["REMESHER_EXTENSION_ID"], "zzamjak_3d_remesher")
            self.assertEqual(payload["argv"][0:3], ["--python-exit-code", "1", "--python"])
            self.assertEqual(payload["argv"][4:], ["--background", "--python-expr", "print('space arg')"])

    def test_macos_runner_replaces_existing_symlink_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            profile_parent = temp_path / "profile"
            wrong_target = temp_path / "other source"
            wrong_target.mkdir()
            link = profile_parent / "5.2/extensions/user_default/zzamjak_3d_remesher"
            link.parent.mkdir(parents=True)
            link.symlink_to(wrong_target)
            fake, capture = self._fake_blender(temp_path)
            env = os.environ.copy()
            env.update(
                {
                    "REMESHER_BLENDER_BINARY": str(fake),
                    "REMESHER_DEV_PROFILE_ROOT": str(profile_parent),
                }
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "dev_run.sh")],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 7)
            self.assertEqual(link.resolve(), ROOT)
            self.assertTrue(capture.exists())

    def test_macos_runner_refuses_real_directory_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            profile_parent = temp_path / "profile"
            collision = profile_parent / "5.2/extensions/user_default/zzamjak_3d_remesher"
            collision.mkdir(parents=True)
            fake, _capture = self._fake_blender(temp_path, exit_code=0)
            env = os.environ.copy()
            env.update(
                {
                    "REMESHER_BLENDER_BINARY": str(fake),
                    "REMESHER_DEV_PROFILE_ROOT": str(profile_parent),
                }
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "dev_run.sh")],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertTrue(collision.is_dir())
            self.assertIn("실제 파일 또는 폴더", result.stderr)

    def test_windows_runner_has_bom_junction_safety_and_exit_forwarding(self) -> None:
        raw = (ROOT / "scripts" / "dev_run.ps1").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        script = raw.decode("utf-8-sig")
        self.assertIn("3DRemesher-Blender\\Blender", script)
        self.assertIn("REMESHER_DEV_PROFILE_ROOT", script)
        self.assertIn("mklink /J", script)
        self.assertIn("ReparsePoint", script)
        self.assertIn("ValueFromRemainingArguments", script)
        self.assertIn("exit $LASTEXITCODE", script)

    def test_cmd_wrapper_is_ascii_and_forwards_args(self) -> None:
        raw = (ROOT / "scripts" / "dev_run.bat").read_bytes()
        raw.decode("ascii")
        self.assertIn(b"%*", raw)
        self.assertIn(b"exit /b %ERRORLEVEL%", raw)

    def test_macos_runner_uses_atomic_symlink_replace(self) -> None:
        script = (ROOT / "scripts" / "dev_run.sh").read_text(encoding="utf-8")
        self.assertIn("os.replace", script)
        self.assertNotIn('rm "${link_path}"', script)
        self.assertNotIn("awk 'NR == 1 {print $2; exit}'", script)


class BuildToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.build_extension = load_build_module()

    def _write_manifest(self, root: Path, extra: str = "") -> None:
        (root / "blender_manifest.toml").write_text(
            "\n".join(
                [
                    'schema_version = "1.0.0"',
                    'id = "zzamjak_3d_remesher"',
                    'version = "0.1.0"',
                    'name = "3D Remesher"',
                    'tagline = "Clean remeshing helpers for Blender"',
                    'maintainer = "zzamjak-cloud"',
                    'type = "add-on"',
                    'tags = ["Mesh"]',
                    'blender_version_min = "4.2.0"',
                    'license = ["SPDX:GPL-3.0-or-later"]',
                    extra,
                ]
            ),
            encoding="utf-8",
        )

    def test_runtime_selection_excludes_development_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_manifest(root)
            (root / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (root / "core.py").write_text("# runtime\n", encoding="utf-8")
            (root / "LICENSE").write_text("GPL-3.0-or-later\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "dev.py").write_text("# dev\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_dev.py").write_text("# test\n", encoding="utf-8")
            (root / ".github").mkdir()
            (root / ".github" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
            (root / "assets").mkdir()
            (root / "assets" / "icon.png").write_bytes(b"png")
            (root / "addon").mkdir()
            (root / "addon" / "__init__.py").write_text("# runtime\n", encoding="utf-8")

            manifest = self.build_extension.load_manifest(root)
            selected = {path.as_posix() for path in self.build_extension.select_runtime_paths(root, manifest)}

        self.assertEqual(selected, {"LICENSE", "__init__.py", "addon/__init__.py", "assets/icon.png", "core.py"})

    def test_declared_build_paths_reject_forbidden_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_manifest(root, '[build]\npaths = ["__init__.py", "scripts/dev_run.sh"]')
            (root / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (root / "scripts").mkdir()
            (root / "scripts" / "dev_run.sh").write_text("# dev\n", encoding="utf-8")
            manifest = self.build_extension.load_manifest(root)

            with self.assertRaises(ValueError):
                self.build_extension.select_runtime_paths(root, manifest)

    def test_build_uses_official_validate_build_validate_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            dist = Path(temp) / "dist"
            self._write_manifest(root)
            (root / "LICENSE").write_text("GPL-3.0-or-later\n", encoding="utf-8")
            (root / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (root / "addon").mkdir()
            (root / "addon" / "__init__.py").write_text("# runtime\n", encoding="utf-8")

            calls: list[list[str]] = []

            def fake_run(_blender_binary: str, command_args: list[str], _cwd: Path) -> None:
                calls.append(command_args)

            original_run = self.build_extension.run_blender_extension
            self.build_extension.run_blender_extension = fake_run
            try:
                output = self.build_extension.build(
                    types.SimpleNamespace(source_dir=root, output_dir=dist, blender_binary="fake-blender")
                )
            finally:
                self.build_extension.run_blender_extension = original_run

        self.assertEqual(output.name, "zzamjak_3d_remesher-v0.1.0.zip")
        self.assertEqual(calls[0], ["validate", str(root.resolve())])
        self.assertEqual(calls[1][0], "build")
        self.assertIn("--source-dir", calls[1])
        self.assertIn("--output-filepath", calls[1])
        self.assertEqual(calls[2], ["validate", str(output)])

    def test_stage_rejects_symlink_escape(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows symlink 권한 차이를 피합니다.")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            stage = Path(temp) / "stage"
            outside = Path(temp) / "outside.py"
            outside.write_text("# outside\n", encoding="utf-8")
            self._write_manifest(root)
            (root / "__init__.py").symlink_to(outside)

            with self.assertRaises(ValueError):
                self.build_extension.stage_runtime_source(root, stage, [Path("__init__.py")])


if __name__ == "__main__":
    unittest.main()
