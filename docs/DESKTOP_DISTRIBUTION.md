# Desktop distribution candidate — release-owner handoff

## Scope and provenance

Based on fetched `origin/main` at `7ee15ca`. The isolated `fix/desktop-distribution` worktree does not incorporate the dirty Pro checkout, other worktrees, or the unmerged calendar/weather widget commit. No release, tag, push, website deployment or public post was performed.

Remote inspection found an existing `v0.1.0.dev1` prerelease with Mac arm64 DMG/PKG, Linux x64 tarball and Windows x64 EXE. These are August legacy-product artifacts, not the current free workspace. Existing native Windows/Linux packaging workflows last ran in August. Do not point the current landing at those binaries.

## Delivered artifacts

All files are in `dist/`, with SHA-256 sidecars. `dist/site/` is a **local review build**, with the three actual portable downloads included. `.evidence/artifacts.json` records exact sizes/hashes.

- `Zhunt-Portable-macos-arm64.tar.gz`: bundled runtime; extract and open `zhunt/Zhunt.command`.
- `Zhunt-Setup-linux-x64.tar.gz`: bundled runtime; extract and run `./zhunt/Start-Zhunt.sh`. Built on real Ubuntu 22.04 WSL2 / glibc 2.35.
- `Zhunt-Portable-win-x64.zip`: bundled runtime; Extract All, then open `zhunt/Start-Zhunt.cmd`. No administrator install required.
- `Zhunt-Setup-macos-arm64.pkg` and `.dmg`: built by the existing native packaging recipe, now with `/Applications/Zhunt.command`; **not installed system-wide or separately validated**. Portable Mac is the tested/recommended path.

The new `zhunt desktop` starts the local daemon on loopback, waits for actual Uvicorn readiness, saves the selected port, and opens the authenticated browser workspace. It stays in the launcher terminal; Ctrl+C stops it. `--no-browser` supports reproducible smoke tests. Windows Start Menu and postinstall actions now use `desktop`, not the previous browser-only `open` action.

## Executed verification

- Full source suite: **737 tests passed**; one existing Starlette/httpx deprecation warning. `.evidence/pytest-final.log`, `.evidence/collected.txt`.
- Mac arm64: extracted actual archive, empty HOME, launched the shipped `.command`; HTTP workspace + authenticated status 200, native Chrome rendered agent workspace and online status, no page errors. `.evidence/mac-smoke-2/`.
- Windows 11 x64 on existing `qc` SSH host: extracted actual ZIP, empty HOME/USERPROFILE, launched shipped `Start-Zhunt.cmd`; HTTP workspace + authenticated status 200, native Windows Chrome rendered workspace and online status, no page errors. `.evidence/windows-launcher-smoke/` (result + screenshot). Remote source/artifacts/evidence live under `D:\zhunt-distribution-review`.
- Linux x64 on `mint-wsl`: extracted actual tarball and ran `Start-Zhunt.sh` with empty HOME; HTTP workspace + authenticated status 200. UI rendered from the real Linux backend through an SSH loopback tunnel in Chrome on macOS, no page errors. This is **not native Linux desktop-browser verification**. `.evidence/linux-smoke/result.json`, `.evidence/linux-ui/`. Remote build lives at `/home/kwf/zhunt-distribution-review`.
- Local landing: clicked all three download buttons in Chrome, downloaded their actual bytes and matched SHA-256; 390px viewport has no horizontal overflow; Android/iOS Under development labels have no links. `.evidence/landing/`.
- Site builder fails before output if any archive/checksum is missing or mismatched. Optional published-release mode downloads and verifies **every public artifact byte-for-byte** before replacing local links.

The UI smoke did not send prompts to providers or purchase anything. Browser handoff itself was suppressed during smoke (`--no-browser`) and Chrome was driven explicitly. Unsigned-file quarantine, Gatekeeper/SmartScreen, native Mac PKG install/uninstall, Intel Mac, Windows ARM and native Linux browser integration are not certified.

## Known functional limitation / release decision

**Integrated Engineering terminals do not work in these frozen portable builds.** The baseline helper launches `[sys.executable, terminal_child.py, ...]`; a frozen executable is not a Python interpreter. The Mac frozen helper probe returned exit 2 (`.evidence/frozen-terminal-probe.log`). Windows already reports integrated terminals unsupported by design. The landing explicitly discloses this and points terminal users to the source install on Mac/Linux. This packaging task did not expand into rewriting Engineering. Release owner must either accept the explicitly limited preview or repair/rebuild/retest terminal dispatch before advertising terminal support.

Workspace startup is verified, not full chat/agent/provider feature certification. Desktop release does not depend on Android/iOS; those labels have no timeline or app-store badges.

## Publication — owner only, after independent QA

1. Review the branch diff and artifacts. Preserve live Pro/Plus experiments: the landing diff edits only installation copy and related install guidance. If live site source is a different branch, integrate these hunks into that source, **do not wholesale deploy an older main-based pricing page**.
2. Publish reviewed code to a remote branch/merge it according to normal policy. Do not change or reuse the legacy `v0.1.0.dev1` release.
3. Example new draft release (the `v0.2.0.dev0` tag matches current project version; verify it is still unused):

```sh
git push origin HEAD:fix/desktop-distribution
gh release create v0.2.0.dev0 --repo loopeywho/zhunt \
  --target "$(git rev-parse HEAD)" --draft --prerelease \
  --title "Zhunt 0.2.0.dev0 desktop preview" \
  --notes-file docs/DESKTOP_RELEASE_NOTES.md \
  dist/Zhunt-Portable-macos-arm64.tar.gz{,.sha256} \
  dist/Zhunt-Setup-linux-x64.tar.gz{,.sha256} \
  dist/Zhunt-Portable-win-x64.zip{,.sha256}
# Independent review first; only then:
gh release edit v0.2.0.dev0 --repo loopeywho/zhunt --draft=false
python scripts/build_site.py --out dist/site-public \
  --download-base-url https://github.com/loopeywho/zhunt/releases/download/v0.2.0.dev0
```

The last command must pass against real public bytes; no guessed or draft-only download URL goes live. Archives exceed the common static-host single-file limit, so deploy `dist/site-public`, not the bundled `dist/site` local-review output. Use the existing authorized website release route and read back exact public install links afterward. This task did not refresh Cloudflare authentication.

## Build/retest commands

```sh
sh packaging/macos/build.sh
# On Linux with Python 3.12:
sh packaging/linux/build.sh
# On Windows PowerShell, portable without installing Inno Setup:
# .\packaging\windows\build.ps1 -SkipInno -Python C:\path\to\python.exe
python scripts/smoke_desktop.py /path/to/extracted/launcher --output /new/test-output
# Add --ui if Playwright and native Chrome are available.
uv run pytest
python scripts/build_site.py
```

Windows CI retains the Inno Setup installer and also uploads the new portable ZIP. Native command failures now stop the PowerShell build. Actual Windows portable artifacts were built before the later fail-fast/path robustness edits to its recipe; runtime source is unchanged. Rebuild the reviewed commit for reproducible release provenance if required by release policy.

## Environment issues encountered

Quill/Chilli SSH endpoints timed out; Mint WSL and QC Windows were reachable. QC C: had almost no free disk; the isolated build/runtime/temp directories were moved to spacious D: without deleting other files. A managed-Python glob matched both an alias and version directory and was corrected. High-latency single-stream artifact transfers were replaced with bounded parallel byte-range transfers; assembled archives were checked against independent build-host checksums. No broad system upgrades or paid signing/enrolment were performed.
