# Windows x64 packaging

The Windows package is built on a real Windows x64 runner. macOS can build the
architecture-neutral Python wheel, but it cannot validate a native `.exe`.

## Build on Windows

Install Python 3.12 x64 and Inno Setup, then from the repository root run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1
```

For a PyInstaller onedir smoke build without Inno Setup:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/build.ps1 -SkipInno
```

The installer is written to `dist/Zhunt-Setup-win-x64.exe`. It installs per-user
under `%LOCALAPPDATA%\Programs\Zhunt` and does not delete `%USERPROFILE%\.zhunt`
on uninstall. Provider keys and the daemon master key are generated or entered
at runtime; no secrets are bundled.

After installation, run the automated localhost smoke check from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/verify.ps1
```

## Required Quill verification

Use a fresh Windows 11 x64 environment and the produced installer, not the repo
or a pre-existing virtual environment:

1. Run `zhunt --help`.
2. Start `zhunt serve` and confirm it stays running for at least 20 seconds.
3. Confirm an unauthenticated request receives HTTP 401.
4. Open `zhunt setup`, configure a real provider key, and confirm the loopback UI.
5. Install and uninstall each applicable recipe; verify backups and byte-for-byte restore.
6. Confirm `%USERPROFILE%\.zhunt\env` and telemetry survive reinstall/uninstall.
7. Check the env file ACL with `icacls`; it must not be readable by other users.

Until this checklist passes on Quill, the Windows installer is a build candidate,
not a release-ready product.
