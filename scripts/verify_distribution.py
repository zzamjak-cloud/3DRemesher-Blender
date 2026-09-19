"""일상 프로필을 변경하지 않고 원격 Extension 설치와 업데이트를 검증한다."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

from build_extension import PROJECT_ID, blender_binary_default


VERIFY_SCRIPT = '''
import importlib
import json
import os
import time
import tomllib
from pathlib import Path
import bpy

profile = Path(os.environ["REMESHER_ACCEPTANCE_PROFILE"])
assert Path(bpy.utils.resource_path("USER")).resolve() == profile.resolve()
repo = next(item for item in bpy.context.preferences.extensions.repos if item.module == "release")
assert repo.remote_url == os.environ["REMESHER_REPOSITORY_URL"]
repo.use_sync_on_startup = True
bpy.context.preferences.system.use_online_access = True
temporary = profile / "temp"
temporary.mkdir(exist_ok=True)
bpy.context.preferences.filepaths.temporary_directory = str(temporary) + os.sep
bpy.ops.wm.save_userpref()
module_name = "bl_ext.release.zzamjak_3d_remesher"
assert module_name in bpy.context.preferences.addons
module = importlib.import_module(module_name)
installed = Path(module.__file__).resolve()
manifest = tomllib.loads((installed.parent / "blender_manifest.toml").read_text(encoding="utf-8"))
assert manifest["version"] == os.environ["REMESHER_EXPECTED_VERSION"]
assert installed.is_relative_to(profile.resolve())
assert not (Path(repo.directory) / "zzamjak_3d_remesher").is_symlink()
bpy.ops.object.select_all(action="DESELECT")
bpy.ops.mesh.primitive_plane_add()
source = bpy.context.active_object
coordinates = tuple(tuple(vertex.co) for vertex in source.data.vertices)
props = bpy.context.scene.zzamjak_3d_remesher
props.target_quad_count = 64
operators = importlib.import_module(module_name + ".addon.operators")
engine_input, warnings = operators._build_engine_input(bpy.context, source)
job = operators._start_worker_job(source, bpy.context.scene, engine_input, warnings)
try:
    deadline = time.monotonic() + 60
    while job.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job.process.poll() == 0, "설치본 worker 실행 실패"
    payload = operators._read_job_result(job)
    assert payload["ok"], payload
    worker_result = operators._deserialize_remesh_result(payload["result"])
    assert worker_result.quality.actual_quad_count == 64
finally:
    if job.process.poll() is None:
        operators._request_job_cancel(job, terminate=True)
    operators._cleanup_job_files(job)
assert bpy.ops.object.zzamjak_3d_remesher_run() == {"FINISHED"}
result = bpy.context.active_object
assert result != source and len(result.data.polygons) == 64
assert all(len(face.vertices) == 4 for face in result.data.polygons)
assert tuple(tuple(vertex.co) for vertex in source.data.vertices) == coordinates
report = {
    "blender_version": bpy.app.version_string,
    "version": os.environ["REMESHER_EXPECTED_VERSION"],
    "remote_url": repo.remote_url,
    "startup_update_check": repo.use_sync_on_startup,
    "enabled": module_name in bpy.context.preferences.addons,
    "installed_inside_isolated_profile": True,
    "worker_quads": worker_result.quality.actual_quad_count,
    "operator_quads": len(result.data.polygons),
    "source_preserved": True,
}
Path(os.environ["REMESHER_ACCEPTANCE_RESULT"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-zip", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--url", default="https://zzamjak-cloud.github.io/3DRemesher-Blender/index.json")
    parser.add_argument("--blender-binary", default=blender_binary_default())
    parser.add_argument("--report", type=Path, default=Path("dist/distribution-verification.json"))
    args = parser.parse_args()
    previous_zip = args.previous_zip.resolve()
    with zipfile.ZipFile(previous_zip) as archive:
        previous = tomllib.loads(archive.read("blender_manifest.toml").decode("utf-8"))
    if previous["id"] != PROJECT_ID or tuple(map(int, previous["version"].split("."))) >= tuple(map(int, args.expected_version.split("."))):
        raise ValueError("같은 Extension의 실제 이전 버전 ZIP이 필요합니다.")

    report = {"previous_version": previous["version"], "expected_version": args.expected_version}
    for scenario in ("upgrade", "fresh_install"):
        with tempfile.TemporaryDirectory(prefix=f"3dremesher-{scenario}-") as temporary:
            profile = Path(temporary)
            env = os.environ.copy()
            env.update({
                "BLENDER_USER_RESOURCES": str(profile),
                "BLENDER_USER_CONFIG": str(profile / "config"),
                "BLENDER_USER_SCRIPTS": str(profile / "scripts"),
                "BLENDER_USER_DATAFILES": str(profile / "datafiles"),
                "BLENDER_USER_EXTENSIONS": str(profile / "extensions"),
                "REMESHER_ACCEPTANCE_PROFILE": str(profile),
                "REMESHER_ACCEPTANCE_RESULT": str(profile / "result.json"),
                "REMESHER_REPOSITORY_URL": args.url,
                "REMESHER_EXPECTED_VERSION": args.expected_version,
                "NO_COLOR": "1",
            })

            def run(*arguments: str) -> str:
                result = subprocess.run(
                    [args.blender_binary, "--background", "--online-mode", *arguments],
                    env=env, capture_output=True, text=True, timeout=180,
                )
                print(result.stdout, end="", flush=True)
                if result.returncode:
                    raise RuntimeError(f"Blender 배포 검증 실패: {arguments}\n{result.stderr}")
                return result.stdout

            def extension(*arguments: str) -> str:
                return run("--command", "extension", *arguments)

            extension("repo-add", "release", "--name", "3D Remesher Release", "--url", args.url,
                      "--directory", str(profile / "extensions" / "release"), "--clear-all")
            package_dir = profile / "extensions" / "release" / PROJECT_ID
            if scenario == "upgrade":
                extension("install-file", str(previous_zip), "--repo", "release", "--enable")
                installed = tomllib.loads((package_dir / "blender_manifest.toml").read_text(encoding="utf-8"))
                if installed["version"] != previous["version"]:
                    raise AssertionError("이전 버전 설치 확인 실패")
                listing = extension("list", "--sync")
                if f"outdated: {previous['version']} -> {args.expected_version}" not in listing:
                    raise AssertionError("실제 원격 업데이트가 제공되지 않았습니다.")
                extension("update", "--sync")
            else:
                extension("install", f"release.{PROJECT_ID}", "--sync", "--enable")
            installed = tomllib.loads((package_dir / "blender_manifest.toml").read_text(encoding="utf-8"))
            if installed["version"] != args.expected_version:
                raise AssertionError("업데이트된 설치 버전 확인 실패")
            check_script = profile / "verify_installed.py"
            check_script.write_text(VERIFY_SCRIPT, encoding="utf-8")
            run("--python-exit-code", "1", "--python", str(check_script))
            report[scenario] = json.loads((profile / "result.json").read_text(encoding="utf-8"))
    report["ok"] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"원격 신규 설치·업데이트 검증 완료: {args.report}")


if __name__ == "__main__":
    main()
