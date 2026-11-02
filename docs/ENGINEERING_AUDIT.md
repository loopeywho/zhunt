# Audit of the Engineering workspace work (Codex / Astra)

Audited 28 October 2026 by Claude Code on branch `feat/engineering-workspace`
(open as PR #5). Scope: the three commits after `38c47ea` (`6efebe9`, `52d4c22`,
`28967b0`), about 1,430 added lines across 30 files. Read-only review plus local
runs; no product code was changed.

## Verdict

The work is real and mostly sound. The terminal runtime, auth, persistence and
translation changes do what the commits and `ENGINEERING_VALIDATION.md` say.
It is **not ready to merge** until three things are settled: the Linux CI
result, the security posture of a new remote-shell surface, and a few
documented-versus-actual gaps (below).

## What was checked and how

| Check | Result |
| --- | --- |
| Full suite, macOS arm64, Python 3.12.13, fresh venv | 727 passed, 1 deprecation warning |
| `scripts/smoke_install.py` (assets, real PTY, persistence) | Passed |
| Real daemon on port 4017, throwaway `HOME`, port 4000 untouched | Unauthenticated `/zhunt/api/engineering` gives 401. Authenticated: project, task, shell session, input, output and stop all worked (`echo HELLO_$((6*7))` returned `HELLO_42`). `/workspace/` served a CSP with a nonce and no `unsafe-inline`. |
| Browser (xterm rendering, tiling, input latency) | **Not exercised by this audit.** Only the validation doc and tests claim it. |
| Provider behaviour with live keys | **Not exercised.** The `ENGINEERING_VALIDATION.md` live-switching table is Codex's own record and could not be re-run here. |

## Findings, most important first

### 1. CI was red on Linux for the first push — FIXED upstream, run `40618247976` green
Run `40617941951` failed on `ubuntu-latest`: 2 failed, 725 passed.
Both failures are in `tests/test_agent_browser.py` ("the agent browser did not
start"). Commit `28967b0` wraps pytest in `xvfb-run` to address it; its run
(`40618247976`) had not finished when this was written. Two things to confirm:
the fix actually turns Linux green, and the "727 passed" claim in the docs holds
on a machine with no display. The suite currently depends on a real browser
being launchable, which is easy to forget on a fresh checkout.

### 2. The master key now grants arbitrary code execution (design risk) — PARTLY FIXED: terminals are now off unless the daemon is bound to loopback
Before this branch the master key could spend your provider quota. Now it also
opens a login shell and Codex/Claude sessions as your user.
- The key is held in `sessionStorage` and passed through the URL fragment, so any
  script that runs in the workspace origin can reach the shell. The CSP
  (`script-src 'self'`) is the main defence, and it is a good one, but it is now
  carrying much more weight.
- `zhunt serve --allow-non-loopback` exists. Nothing in `engineering.py` checks
  the bind address, so with that flag the new endpoints become a remote shell
  protected only by one bearer key over plain HTTP.
- Suggested fix: refuse to mount `/zhunt/api/engineering` when the daemon is bound
  beyond loopback, or require an explicit opt-in flag for it. Mention it in
  `GETTING_STARTED.md`, which currently only says "keep the daemon local".

### 3. Environment scrubbing is a name denylist (low) — DOCS FIXED (WORKSPACE_API now says denylist)
`launch()` drops variables whose names contain `API_KEY`, `MASTER_KEY`, `TOKEN`,
`SECRET` or `PASSWORD`. Names such as `AWS_ACCESS_KEY_ID`, `DATABASE_URL`,
`GITHUB_PAT` or `NPM_CONFIG__AUTH` pass straight through. The shell already runs
as the user, so this is not an escape, but the docs say secrets are filtered,
which overstates it. Either say "common secret names" or switch to an allowlist
for `-zhunt` kinds. The `codex-zhunt`/`hermes-zhunt` children do receive the
master key by design (`ZHUNT_API_KEY`), which is what the test asserts.

### 4. A corrupt `engineering.json` stops the whole daemon (medium) — FIXED (kept as `.bad`, starts empty, notice shown)
`Engineering.__init__` raises on invalid JSON or wrong shape, and
`mount_workspace` builds it during app startup. A half-written or hand-edited file
therefore stops routing too, not just the Engineering window. `save()` writes a
temp file and renames, which helps, but it has no `fsync`. Suggested fix: on a
bad file, move it aside to `engineering.json.bad`, start empty, and show a notice.

### 5. Tasks and worktrees can be created but never removed (medium)
There is no delete endpoint for tasks or projects. Each isolated task leaves a
branch `zhunt/<id>` in the project and a directory under `~/.zhunt/worktrees/`
forever. The docs do not say this. Add task removal that runs
`git worktree remove`, or at least document the manual cleanup.

### 6. "Stop terminates the process and its children" is slightly too strong (low) — DOCS FIXED
`Session.stop()` takes a `ps` snapshot of descendants, then kills them and the
process group. A child that has already double-forked and been reparented to PID 1
is not found. The tests cover `sleep 120 &`, which is the common case. Soften the
wording or document the limit. `stop()` also holds the session lock while `ps`
runs (up to 3 s), which blocks output for that session.

### 7. Smaller items
- The CSP style nonce is generated once per daemon process and returned by the
  authenticated state endpoint, not generated per response. Impact is small
  (styles only, script policy unchanged) but it is weaker than a normal nonce.
- The terminal polls every 200 ms per open Engineering window and sends one HTTP
  request per input chunk. Fine for local use; expect latency and idle chatter
  with several windows.
- `git status` / `git diff` run inside user-registered directories. A hostile
  repository config such as `core.fsmonitor` can execute code on `git status`.
  `--no-ext-diff --no-textconv` is applied for diff, but not that. Low risk
  because the user chose the folder.
- Keyboard shortcuts are all suppressed while the terminal has focus (`keys.js`).
  Intentional and noted in a comment, but there is no on-screen hint.
- The vendored `xterm.js` / `addon-fit.js` are minified with no version string I
  could find. `THIRD_PARTY_NOTICES.md` says 6.0.0 and 0.11.0; that is unverified.
  Add a checksum or a `vendor/README` stating where they were fetched.
- `README.md` and `GETTING_STARTED.md` install from
  `git+https://github.com/loopeywho/zhunt`. The repo is public, but the install
  command pulls `main`, which does not contain this work until PR #5 merges.

## Things confirmed as correct

- Every engineering route sits behind the existing master-key check, with a test.
- PTY handling is careful: controlling TTY set via a helper process instead of
  `preexec_fn` in a threaded server, bounded 1 MiB scrollback with offset/reset
  semantics, input size caps, 16 running / 64 retained session limits.
- Daemon shutdown stops sessions; a background job started in a shell was shown
  to die with it.
- `engineering.json` is mode 0600; sessions are memory-only and never auto-run
  anything after a restart.
- The `llm.py` / `translate.py` changes (empty streams now fail so the retry
  engine can switch models, stream usage requested and read from
  `response.completed`, Responses custom-tool bridge, exact-match channel-marker
  repair) match their tests and the validation doc, and do not widen tool
  permissions.
- No Claude wiring was added to Zhunt's own config. The `claude` terminal kind
  just runs the user's installed CLI with its own account, which is consistent
  with the earlier decision not to route Claude through Zhunt.

## Suggested order of work

1. Wait for CI run `40618247976`; fix anything still red on Linux.
2. Gate the engineering routes on loopback binding (finding 2).
3. Make a bad `engineering.json` non-fatal (4); add task/worktree removal (5).
4. Tighten the docs wording on scrubbing and Stop (3, 6) and pin the vendored
   xterm files (7).
5. Do one human pass in a real browser: floating and tiled windows, paste,
   resize, reload while a session runs.

## Housekeeping noticed

- `uv.lock` and `.claude/` are untracked in the working tree. Decide whether to
  commit or ignore them.
- Local branches `fix/agent-model-label-gemini` and `fix/keyless-first-run`
  are already merged (PRs #3 and #4) and can be deleted.

## Fix log

- 2026-10-28: terminals gated on loopback bind (`server.run_proxy` → `create_proxy_app(engineering=...)`,
  disabled mode in `engineering.mount_engineering`, tile shows the reason); damaged
  `engineering.json` moved aside instead of stopping startup; docs wording for
  scrubbing and Stop. Suite: 733 passed. Still open: finding 5 (task/worktree
  removal), CSP nonce per response, vendored xterm provenance, browser pass.
