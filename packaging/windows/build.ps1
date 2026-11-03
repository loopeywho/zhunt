param(
    [switch]$SkipInno,
    [string]$Python = ""
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

if ($Python) {
    & $Python -m venv $venv
} else {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -eq $py) {
        throw "Python 3.12 x64 is required. Pass -Python with its full executable path."
    }
    & $py.Source -3.12 -m venv $venv
}
if ($LASTEXITCODE -ne 0) { throw "Creating the build environment failed." }
$python = Join-Path $venv "Scripts\python.exe"
& $python -c "import platform, struct, sys; assert sys.version_info[:2] == (3, 12); assert struct.calcsize('P') == 8; print(platform.platform())"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 x64 is required." }
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed." }
& $python -m pip install "$root" "pyinstaller>=6.10,<7"
if ($LASTEXITCODE -ne 0) { throw "Installing build dependencies failed." }

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name zhunt `
    --distpath $dist `
    --workpath $work `
    --collect-all zhunt `
    --collect-all PIL `
    --collect-all fastapi `
    --collect-all uvicorn `
    --collect-all typer `
    --collect-all ruamel.yaml `
    --collect-all yaml `
    --add-data ((Join-Path $root "zhunt\catalog.yaml") + ";zhunt") `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    (Join-Path $root "packaging\windows\entrypoint.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$package = Join-Path $dist "zhunt"
if (-not (Test-Path (Join-Path $package "zhunt.exe"))) {
    throw "PyInstaller did not produce zhunt.exe."
}

Copy-Item (Join-Path $root "README.md") $package
Copy-Item (Join-Path $root "LICENSE") $package
Copy-Item (Join-Path $root "packaging\windows\Start-Zhunt.cmd") $package
$output = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$archive = Join-Path $output "Zhunt-Portable-win-x64.zip"
Compress-Archive -Path $package -DestinationPath $archive -Force
((Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant() + "  " + (Split-Path -Leaf $archive)) | Set-Content "${archive}.sha256"

if (-not $SkipInno) {
    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -eq $iscc) {
        $knownIscc = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($knownIscc) {
            $iscc = Get-Command $knownIscc
        }
    }
    if ($null -eq $iscc) {
        throw "Inno Setup (ISCC.exe) is required to create the installer. Use -SkipInno for an onedir smoke build."
    }
    & $iscc.Source (Join-Path $root "packaging\windows\zhunt.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
    $installer = Join-Path $root "dist\Zhunt-Setup-win-x64.exe"
    (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLowerInvariant() | Set-Content `
        -NoNewline ("${installer}.sha256")
}

Write-Host "Windows x64 build complete: $package"
