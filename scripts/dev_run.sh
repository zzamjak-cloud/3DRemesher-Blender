#!/usr/bin/env bash
set -euo pipefail

extension_id="${REMESHER_EXTENSION_ID:-zzamjak_3d_remesher}"
repo_module="${REMESHER_REPO_MODULE:-user_default}"
blender_binary="${REMESHER_BLENDER_BINARY:-${BLENDER_BINARY:-/Applications/Blender.app/Contents/MacOS/Blender}}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
manifest_path="${repo_root}/blender_manifest.toml"
bootstrap_path="${script_dir}/dev_bootstrap.py"

if [[ ! -f "${manifest_path}" ]]; then
  echo "blender_manifest.toml을 찾을 수 없습니다: ${manifest_path}" >&2
  exit 1
fi

if [[ ! -x "${blender_binary}" ]]; then
  echo "Blender 실행 파일을 찾을 수 없습니다: ${blender_binary}" >&2
  echo "REMESHER_BLENDER_BINARY 또는 BLENDER_BINARY로 경로를 지정하세요." >&2
  exit 1
fi

version="$("${blender_binary}" --version | awk 'NR == 1 {version=$2} END {print version}')"
major_minor="$(printf '%s\n' "${version}" | awk -F. '{print $1 "." $2}')"
if [[ ! "${major_minor}" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "Blender 버전을 확인할 수 없습니다: ${version}" >&2
  exit 1
fi

profile_parent="${REMESHER_DEV_PROFILE_ROOT:-${HOME}/Library/Application Support/Blender/3DRemesherDev}"
profile_root="${profile_parent}/${major_minor}"
repo_dir="${profile_root}/extensions/${repo_module}"
link_path="${repo_dir}/${extension_id}"

mkdir -p "${repo_dir}" "${profile_root}/config" "${profile_root}/scripts" "${profile_root}/datafiles"

python3 - "${link_path}" "${repo_root}" <<'PY'
import os
import sys
from pathlib import Path

link_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
temp_link = link_path.with_name(f".{link_path.name}.tmp.{os.getpid()}")

try:
    if os.path.lexists(link_path):
        if not link_path.is_symlink():
            raise SystemExit(f"링크 위치에 실제 파일 또는 폴더가 있어 중단합니다: {link_path}")
        if Path(os.path.realpath(link_path)) == repo_root:
            raise SystemExit(0)

    if os.path.lexists(temp_link):
        temp_link.unlink()
    os.symlink(str(repo_root), str(temp_link))
    os.replace(str(temp_link), str(link_path))
finally:
    if os.path.lexists(temp_link):
        temp_link.unlink()
PY

export REMESHER_EXTENSION_ID="${extension_id}"
export REMESHER_REPO_MODULE="${repo_module}"
export REMESHER_DEV_ROOT="${repo_root}"
export REMESHER_DEV_PROFILE="${profile_root}"
export BLENDER_USER_RESOURCES="${profile_root}"
export BLENDER_USER_CONFIG="${profile_root}/config"
export BLENDER_USER_SCRIPTS="${profile_root}/scripts"
export BLENDER_USER_DATAFILES="${profile_root}/datafiles"
export BLENDER_USER_EXTENSIONS="${profile_root}/extensions"

echo "3D Remesher 개발 프로필: ${profile_root}"
echo "3D Remesher 소스 링크: ${link_path} -> ${repo_root}"

exec "${blender_binary}" --python-exit-code 1 --python "${bootstrap_path}" "$@"
