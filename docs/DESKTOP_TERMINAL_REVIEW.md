# Frozen Engineering terminal review

## Outcome and provenance

The prior frozen Mac blocker was reproduced via the authenticated Engineering API:
the original executable treated `terminal_child.py` as an unknown CLI command.
The rebuilt Mac arm64 portable archive now passes a real PTY smoke. This is a
local repair candidate, not approval to publish or merge.

Worktree: `/Users/loopey/.hermes/profiles/timothy/cache/zhunt-terminal-review`
Branch: `fix/frozen-engineering-terminal`, based on
`4470bc6e99e76e84d175097d5d9a3549b36c445a`. Changes are left uncommitted for parent
independent review. The baseline worktree and its artifacts remain unchanged.

## Inspected and repaired

- `engineering.Session` previously passed a script filename to `sys.executable`.
  In PyInstaller that executable runs Zhunt, not Python. Frozen sessions now use
  one exact private dispatch flag; source sessions retain script invocation.
- Shared packaging entry point dispatches the helper before CLI/server imports.
  It accepts the flag only in the first argument position and only when frozen.
- Helper lazily imports POSIX modules (normal Windows startup stays importable),
  rejects missing commands/non-POSIX, acquires the controlling PTY before exec,
  and restores the original Linux loader path before launching external programs.
- No shell-string interpolation or `preexec_fn`; existing auth, loopback-only
  guard, environment filtering, PTY session creation, and stop logic are unchanged.
- `tests/test_frozen_terminal.py` adds dispatch, spawn-vector/FD cleanup,
  fail-closed TTY, argument preservation and loader-path regression coverage.
- `scripts/smoke_desktop.py --terminal` uses the new `scripts/smoke_terminal.py`
  to exercise the shipped service, not imported source modules.

## Executed evidence

All evidence paths below are relative to this worktree.

- `.evidence/artifact-audit.json`: independently recomputed every original Mac,
  Linux and Windows artifact size/SHA-256, matched all five recorded artifacts
  and their sidecars. Summed original collection evidence to 737 tests.
  Original `pytest-final.log` contains a complete passing progress run but no
  explicit total line; the collection file is the source of that original total.
- `.evidence/baseline-terminal-smoke.log`: actual original packaged terminal
  failure, reproduced independently with the new API smoke.
- `.evidence/pytest-final.log`: **749 passed**, one existing Starlette/httpx
  deprecation warning. Includes existing real source PTY/shutdown/auth tests.
- `.evidence/mac-build.log`: successful native `sh packaging/macos/build.sh`.
- `.evidence/mac-terminal-final/result.json`: extracted the rebuilt archive,
  launched its `Zhunt.command` in a new HOME/USERPROFILE, workspace and
  authenticated status HTTP 200; unauthenticated Engineering requests rejected;
  real shell wrote a unique nonce file in a project path containing spaces;
  stdin/out/err are TTYs, `/dev/tty` works, resize reports 39 rows/117 columns;
  Ctrl+C interrupts a foreground sleep; stop kills shell and background sleep.
  Exit -9 is expected for the explicit stop operation. No providers were called.
- `.evidence/mac-terminal-final/workspace.png`: native Chrome rendered the
  online workspace with zero page errors. Terminal interaction was exercised
  through the authenticated API, not a browser terminal keyboard/UI test.
- `.evidence/artifact-audit.json` also records real frozen negative probes:
  normal `--help` exits 0; missing helper command and non-TTY execution exit 1;
  non-TTY probe does not execute its shell command.

## Rebuilt local artifacts

| Artifact in `dist/` | Bytes | SHA-256 |
|---|---:|---|
| `Zhunt-Portable-macos-arm64.tar.gz` | 28402824 | `c4366ff74ceab010c60b56726e95715bf1a143c17d7e4996b9b3b1d78733225e` |
| `Zhunt-Setup-macos-arm64.pkg` | 28447693 | `d230080a53fea3cf3b17237a7d61150bb5abf4cff38e93b33e0d14c62d020c9f` |
| `Zhunt-Setup-macos-arm64.dmg` | 28770053 | `2bd2e24bd7dbfa41f36e2f7eb90ed351dd7aba3ffd69b1bd5a5d83f94d063b0b` |

Sidecars match actual bytes. Runtime source hashes are recorded in the audit;
only tests/smoke/docs were refined after the binary build.

## Remaining gates / limits

- Parent must independently review this repair diff; the implementer does not
  certify their own work for merge. No push, tag, release, deployment or spending.
- Linux has now been rebuilt and passed the shipped-launcher PTY smoke on
  `mint-wsl`; see the Linux follow-up below. The earlier Linux candidate under
  `.evidence/linux-repair/` remains a known failed artifact, not repaired evidence.
- Windows integrated terminals remain intentionally unsupported. Windows was
  not rebuilt after this entry-point edit; a new Windows startup smoke is needed
  before releasing a rebuilt Windows binary.
- Mac PKG/DMG were built but not installed/uninstalled. Signing, Gatekeeper,
  quarantine/browser handoff, Intel Mac, native Linux browser integration,
  provider/agent commands and full terminal UI interaction remain uncertified.
- Landing remains the baseline preview; release notes distinguish the repaired
  local Mac/Linux candidates from baseline bytes. Do not mix these local
  bytes into a public download set without fresh hashes/site QA and an
  explicit platform-specific support statement. Plus/mobile trees are untouched.

## Linux packaged-terminal follow-up

The previous Linux attempt failed with `No such option: --zhunt-terminal-child`:
`packaging/linux/build.sh` uses its own entrypoint, not the shared Windows/Mac
entrypoint. `packaging/linux/entrypoint.py` now applies the same exact, frozen-only
first-argument dispatch before importing CLI/server code. Auth and loopback
safeguards are unchanged. No shared runtime or Mac entrypoint changed in this
follow-up, so the recorded Mac artifacts remain applicable.

Regression tests now discover each Linux/macOS/Windows entrypoint from its actual
build recipe, check normal/nonfrozen/misplaced/near-match flags, and execute each
`__main__` path with CLI/server imports forbidden during helper dispatch.
The final full source suite is **765 passed**, one existing deprecation warning
(`.evidence/linux-terminal-final/pytest.log`).

Rebuilt using Python 3.12.16 on `mint-wsl`, in
`/home/kwf/zhunt-distribution-review/terminal-repair/source`, using the unchanged
Linux recipe. Extracted to `../extracted-final`, then ran:

```sh
.linux-build/venv/bin/python scripts/smoke_desktop.py \
  ../extracted-final/zhunt/Start-Zhunt.sh --terminal \
  --output ../linux-terminal-final
```

`.evidence/linux-terminal-final/result.json` records the passing clean-HOME
shipped-launcher smoke on WSL2 x86_64/glibc 2.35: workspace/authenticated status
200, unauthenticated Engineering rejected, unique file written by a real shell,
cwd containing spaces, controlling `/dev/tty`, resize to 117 columns/39 rows,
Ctrl+C interrupt, and shell/background-job cleanup. Exit -9 is the intentional
stop. This is authenticated API-driven PTY verification, **not browser keyboard
interaction or native Linux browser integration**. No provider calls were made.

The passing archive was retrieved only after reading the remote smoke result.
Bounded parallel transfer, independent remote SHA-256, sidecar and local readback
all match (`artifact-verification.json`). `source-manifest.json` matches local and
remote entrypoints, build recipe, runtime helper, Engineering and smoke/test code;
`linux-build.log`, `linux-terminal-smoke.log` and `startup.log` preserve evidence.
The earlier failed `.evidence/linux-repair/` files are retained unchanged.

| Passing local artifact in `dist/` | Bytes | SHA-256 |
|---|---:|---|
| `Zhunt-Setup-linux-x64.tar.gz` | 33840375 | `17acb718543f2e7c8142e49a0ba12f3707504f2ac394add7e97e682cb2d3f369` |

Matching `.sha256` is beside the archive. No publication, push, release, spending,
Windows host probes, model/auth configuration changes or unrelated worktree edits
were performed.

## Reproduce

```sh
uv run pytest -o addopts=''
sh packaging/macos/build.sh
mkdir -p .evidence/extracted
# Use a new extraction directory if repeating against a different archive.
tar -xzf dist/Zhunt-Portable-macos-arm64.tar.gz -C .evidence/extracted
uv run --with playwright python scripts/smoke_desktop.py \
  .evidence/extracted/zhunt/Zhunt.command --terminal --ui \
  --output .evidence/new-mac-terminal-smoke
```
