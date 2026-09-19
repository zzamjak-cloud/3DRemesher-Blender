param(
    [string]$BlenderBinary = $env:REMESHER_BLENDER_BINARY,
    [switch]$Background,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BlenderArgs
)

$ErrorActionPreference = "Stop"

$extensionId = if ($env:REMESHER_EXTENSION_ID) { $env:REMESHER_EXTENSION_ID } else { "zzamjak_3d_remesher" }
$repoModule = if ($env:REMESHER_REPO_MODULE) { $env:REMESHER_REPO_MODULE } else { "user_default" }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
$manifestPath = Join-Path $repoRoot "blender_manifest.toml"
$bootstrapPath = Join-Path $scriptDir "dev_bootstrap.py"

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "blender_manifest.toml을 찾을 수 없습니다: $manifestPath"
}

if (-not $BlenderBinary) {
    $localCandidates = @(
        (Join-Path $repoRoot "Blender\blender.exe"),
        (Join-Path $repoRoot "tools\Blender\blender.exe")
    )
    foreach ($candidate in $localCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $BlenderBinary = $candidate
            break
        }
    }
}
if (-not $BlenderBinary) {
    $command = Get-Command "blender.exe" -ErrorAction SilentlyContinue
    if ($command) {
        $BlenderBinary = $command.Source
    }
}
if (-not $BlenderBinary -or -not (Test-Path -LiteralPath $BlenderBinary -PathType Leaf)) {
    throw "Blender 실행 파일을 찾을 수 없습니다. REMESHER_BLENDER_BINARY를 지정하세요."
}

$versionText = & $BlenderBinary --version | Select-Object -First 1
if ($versionText -notmatch "Blender\s+([0-9]+)\.([0-9]+)") {
    throw "Blender 버전을 확인할 수 없습니다: $versionText"
}
$majorMinor = "$($Matches[1]).$($Matches[2])"

$profileParent = if ($env:REMESHER_DEV_PROFILE_ROOT) {
    $env:REMESHER_DEV_PROFILE_ROOT
} else {
    Join-Path $env:LOCALAPPDATA "3DRemesher-Blender\Blender"
}
$profileRoot = Join-Path $profileParent $majorMinor
$repoDir = Join-Path $profileRoot "extensions\$repoModule"
$linkPath = Join-Path $repoDir $extensionId

New-Item -ItemType Directory -Force -Path $repoDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $profileRoot "config") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $profileRoot "scripts") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $profileRoot "datafiles") | Out-Null

if (Test-Path -LiteralPath $linkPath) {
    $item = Get-Item -LiteralPath $linkPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
        throw "링크 위치에 실제 파일 또는 폴더가 있어 중단합니다: $linkPath"
    }
    $target = $item.Target
    if ($target -is [array]) {
        $target = $target[0]
    }
    if ((Resolve-Path -LiteralPath $target).Path -ne (Resolve-Path -LiteralPath $repoRoot).Path) {
        Remove-Item -LiteralPath $linkPath -Force
    }
}

if (-not (Test-Path -LiteralPath $linkPath)) {
    $mklink = cmd.exe /c mklink /J "$linkPath" "$repoRoot"
    if ($LASTEXITCODE -ne 0) {
        throw "Junction 생성 실패: $mklink"
    }
}

$env:REMESHER_EXTENSION_ID = $extensionId
$env:REMESHER_REPO_MODULE = $repoModule
$env:REMESHER_DEV_ROOT = $repoRoot
$env:REMESHER_DEV_PROFILE = $profileRoot
$env:BLENDER_USER_RESOURCES = $profileRoot
$env:BLENDER_USER_CONFIG = Join-Path $profileRoot "config"
$env:BLENDER_USER_SCRIPTS = Join-Path $profileRoot "scripts"
$env:BLENDER_USER_DATAFILES = Join-Path $profileRoot "datafiles"
$env:BLENDER_USER_EXTENSIONS = Join-Path $profileRoot "extensions"

Write-Host "3D Remesher 개발 프로필: $profileRoot"
Write-Host "3D Remesher 소스 링크: $linkPath -> $repoRoot"

$argsList = @("--python-exit-code", "1", "--python", $bootstrapPath)
if ($Background) {
    $argsList += "--background"
}
$argsList += $BlenderArgs

& $BlenderBinary @argsList
exit $LASTEXITCODE
