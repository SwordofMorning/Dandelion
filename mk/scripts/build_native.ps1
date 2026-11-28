<#
@file mk/scripts/build_native.ps1
@date 2026/11/28
@brief Windows PowerShell helper for building packages/dandelion-core via CMake + Ninja + MSVC (cl.exe).

Usage:
    powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target dev
    powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target build
    powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target wheel
    powershell -ExecutionPolicy Bypass -File mk/scripts/build_native.ps1 -Target clean
#>

[CmdletBinding()]
param(
    [ValidateSet("build", "dev", "clean", "wheel")]
    [string]$Target = "build",
    [string]$Python = "python",
    [string]$OutputDir = "dist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$CoreDir = Join-Path $RootDir "packages\dandelion-core"
$WheelDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $RootDir $OutputDir }

function Import-MsvcEnv {
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) {
        Write-Host "[+] MSVC cl.exe already available in PATH."
        return
    }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        throw "cl.exe not found in PATH and vswhere.exe not found. Open a Visual Studio x64 Developer Prompt or install Visual Studio C++ Build Tools."
    }

    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $vsPath) {
        throw "Visual Studio with C++ x64 build tools (VC.Tools.x86.x64) was not found (vswhere exit $LASTEXITCODE)."
    }

    $vsDevCmd = Join-Path $vsPath "Common7\Tools\VsDevCmd.bat"
    if (-not (Test-Path $vsDevCmd)) {
        throw "VsDevCmd.bat not found at: $vsDevCmd"
    }

    Write-Host "[*] Importing MSVC x64 developer environment from: $vsPath"
    $envDump = cmd.exe /s /c "`"$vsDevCmd`" -arch=amd64 -host_arch=amd64 -no_logo && set"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to initialize MSVC environment via VsDevCmd.bat (exit $LASTEXITCODE)."
    }

    foreach ($line in $envDump) {
        $eq = $line.IndexOf("=")
        if ($eq -gt 0) {
            $name = $line.Substring(0, $eq)
            $val = $line.Substring($eq + 1)
            [System.Environment]::SetEnvironmentVariable($name, $val, "Process")
        }
    }

    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        throw "MSVC cl.exe is still unavailable after importing VsDevCmd.bat."
    }
    Write-Host "[+] MSVC cl.exe ready: $((Get-Command cl.exe).Source)"
}

function Ensure-CMakeNinja {
    $pyScripts = & $Python -c "import os, sys; print(os.path.join(sys.prefix, 'Scripts'))"
    if ($LASTEXITCODE -eq 0 -and $pyScripts -and (Test-Path $pyScripts)) {
        if (-not ($env:PATH.Split(";") -contains $pyScripts)) {
            $env:PATH = "$pyScripts;$env:PATH"
        }
    }

    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
        throw "cmake was not found in PATH. Install CMake or run: $Python -m pip install cmake ninja"
    }

    if (Get-Command ninja -ErrorAction SilentlyContinue) {
        $env:CMAKE_GENERATOR = "Ninja"
        Write-Host "[+] Using CMake + Ninja -> cl.exe"
    } else {
        Write-Host "[!] Ninja not found in PATH; falling back to default CMake generator."
    }
}

function Test-LocalCoreBuildDeps {
    & $Python -c "import importlib.util, sys; sys.exit(0 if (importlib.util.find_spec('scikit_build_core') and importlib.util.find_spec('nanobind')) else 1)"
    return ($LASTEXITCODE -eq 0)
}

function Assert-LocalCoreDirectUrl {
    param(
        [string]$ExpectedRoot
    )
    $checkCode = @'
import importlib.metadata as m, json, os, sys
from urllib.parse import urlparse
from urllib.request import url2pathname

raw = m.distribution("dandelion-core").read_text("direct_url.json")
assert raw, "Missing PEP 610 direct_url.json (installed dandelion-core is not local)"
info = json.loads(raw)
url = info.get("url", "")
parsed = urlparse(url)
assert parsed.scheme == "file", f"Non-local direct_url scheme: {url}"
local_path = os.path.realpath(url2pathname(parsed.path))
expected = os.path.realpath(sys.argv[1])
assert local_path == expected or local_path.startswith(expected + os.sep), f"Installed dandelion-core outside expected root: {local_path} vs {expected}"
print("[+] Verified PEP 610 direct_url.json:", local_path)
'@
    & $Python -I -c $checkCode $ExpectedRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Local dandelion-core direct_url.json verification failed."
    }
}

if ($Target -eq "clean") {
    Write-Host "[*] Cleaning native build artifacts..."
    $toClean = @(
        (Join-Path $CoreDir "build"),
        (Join-Path $RootDir "build"),
        $WheelDir
    )
    foreach ($p in $toClean) {
        if (Test-Path $p) {
            Remove-Item -Recurse -Force $p
            Write-Host "[+] Removed $p"
        }
    }
    Get-ChildItem -Path $CoreDir -Recurse -Filter "_native*.pyd" -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item -Force $_.FullName
        Write-Host "[+] Removed $($_.FullName)"
    }
    Write-Host "[+] Clean complete."
    exit 0
}

if (-not (Test-Path $CoreDir)) {
    throw "Core package directory not found: $CoreDir"
}

Import-MsvcEnv
Ensure-CMakeNinja

$HasLocalDeps = Test-LocalCoreBuildDeps
if ($HasLocalDeps) {
    Write-Host "[+] Found local scikit-build-core and nanobind (using --no-build-isolation)."
} else {
    Write-Host "[*] Local scikit-build-core/nanobind not detected (using PEP 517 build isolation)."
}

if ($Target -eq "build") {
    Write-Host "[*] Building and installing dandelion-core editable..."
    $pipArgs = @("-m", "pip", "install")
    if ($HasLocalDeps) {
        $pipArgs += "--no-build-isolation"
    }
    $pipArgs += @("--no-deps", "-e", $CoreDir)
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Native editable install failed with exit code $LASTEXITCODE."
    }
    Assert-LocalCoreDirectUrl -ExpectedRoot $CoreDir
    & $Python -I -c "from dandelion_core.utils.llm_provider import _native; print('[+] Loaded native module:', _native.__file__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Native module verification import failed."
    }
    Write-Host "[+] Native build succeeded."
    exit 0
}

if ($Target -eq "dev") {
    Write-Host "[*] Installing local dandelion-core + root dev dependencies (local-first)..."
    $pipArgs = @("-m", "pip", "install")
    if ($HasLocalDeps) {
        $pipArgs += "--no-build-isolation"
    }
    Push-Location $RootDir
    try {
        $pipArgs += @("-e", $CoreDir, "-e", ".[dev]")
        & $Python @pipArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Local dev install failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
    Assert-LocalCoreDirectUrl -ExpectedRoot $CoreDir
    & $Python -I -c "from dandelion_core.utils.llm_provider import _native; print('[+] Loaded native module:', _native.__file__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Native module verification import failed."
    }
    Write-Host "[+] Local dev environment ready."
    exit 0
}

if ($Target -eq "wheel") {
    New-Item -ItemType Directory -Force -Path $WheelDir | Out-Null
    Write-Host "[*] Building dandelion-core wheel into $WheelDir ..."
    $pipArgs = @("-m", "pip", "wheel")
    if ($HasLocalDeps) {
        $pipArgs += "--no-build-isolation"
    }
    $pipArgs += @("--no-deps", "--wheel-dir", $WheelDir, $CoreDir)
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Wheel build failed with exit code $LASTEXITCODE."
    }
    $builtWheels = @(Get-ChildItem -Path $WheelDir -Filter "dandelion_core-*.whl" | Sort-Object LastWriteTime -Descending)
    if ($builtWheels.Count -eq 0) {
        throw "No dandelion_core-*.whl artifact found in $WheelDir."
    }
    foreach ($w in $builtWheels) {
        Write-Host "[+] Built wheel: $($w.FullName)"
    }

    # Verify the newly built wheel installs and imports _native inside an isolated venv.
    $verifyDir = Join-Path $env:TEMP ("dandelion-wheel-verify-" + $PID)
    try {
        if (Test-Path $verifyDir) {
            Remove-Item -Recurse -Force $verifyDir
        }
        & $Python -m venv $verifyDir
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create verification venv at $verifyDir."
        }
        $verifyPy = Join-Path $verifyDir "Scripts\python.exe"
        & $verifyPy -m pip install --no-deps --force-reinstall $builtWheels[0].FullName
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install built wheel into verification venv."
        }
        & $verifyPy -I -c "import os, sys; from dandelion_core.utils.llm_provider import _native; p = os.path.abspath(_native.__file__); assert p.startswith(os.path.abspath(sys.prefix)), p; print('[+] Verified wheel _native:', p)"
        if ($LASTEXITCODE -ne 0) {
            throw "Built wheel failed isolated _native import verification."
        }
    } finally {
        if (Test-Path $verifyDir) {
            Remove-Item -Recurse -Force $verifyDir
        }
    }
    exit 0
}
