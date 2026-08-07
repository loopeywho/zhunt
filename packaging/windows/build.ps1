param(
    [switch]$SkipInno
)

$ErrorActionPreference = "Stop"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Zhunt Windows packages require a 64-bit Windows host."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\.." )).Path
$buildRoot = Join-Path $root ".windows-build"
$venv = Join-Path $buildRoot "venv"
$dist = Join-Path $buildRoot "dist"
$work = Join-Path $buildRoot "work"

if (Test-Path $buildRoot) {
    Remove-Item -Recurse -Force $buildRoot
}
New-Item -ItemType Directory -Path $buildRoot | Out-Null

$py = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $py) {
    throw "Python Launcher (py.exe) is required. Install Python 3.12 x64 from python.org and retry."
}

& $py.Source -3.12 -m venv $venv
$python = Join-Path $venv "Scripts\python.exe"
& $python -c "import platform, struct, sys; assert sys.version_info[:2] == (3, 12); assert struct.calcsize('P') == 8; print(platform.platform())"
& $python -m pip install --upgrade pip
& $python -m pip install "." "pyinstaller>=6.10,<7"

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name zhunt `
    --distpath $dist `
    --workpath $work `
    --collect-all zhunt `
    --collect-all litellm `
    --collect-all fastapi `
    --collect-all uvicorn `
    --collect-all typer `
    --collect-all ruamel.yaml `
    --collect-all yaml `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    (Join-Path $root "packaging\windows\entrypoint.py")

$package = Join-Path $dist "zhunt"
if (-not (Test-Path (Join-Path $package "zhunt.exe"))) {
    throw "PyInstaller did not produce zhunt.exe."
}

Copy-Item (Join-Path $root "README.md") $package
Copy-Item (Join-Path $root "LICENSE") $package

if (-not $SkipInno) {
    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -eq $iscc) {
        $knownIscc = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($knownIscc) {
            $iscc = Get-Item $knownIscc
        }
    }
    if ($null -eq $iscc) {
        throw "Inno Setup (ISCC.exe) is required to create the installer. Use -SkipInno for an onedir smoke build."
    }
    & $iscc.Source (Join-Path $root "packaging\windows\zhunt.iss")
    $installer = Join-Path $root "dist\Zhunt-Setup-win-x64.exe"
    (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLowerInvariant() | Set-Content `
        -NoNewline ("${installer}.sha256")
}

Write-Host "Windows x64 build complete: $package"
