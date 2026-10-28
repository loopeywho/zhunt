# Getting started with the Zhunt developer preview

This preview adds real terminal project/task workspaces to Zhunt's free-first
router and floating/tiled desktop. Use the source installation below on macOS or
Linux. Python 3.12 or newer and Git are required. Windows terminal support and
signed native installers are not included in this preview.

## Install and start

With [uv](https://docs.astral.sh/uv/) installed:

```sh
uv tool install --python 3.12 git+https://github.com/loopeywho/zhunt
zhunt serve
```

Keep the daemon running. In another terminal, run `zhunt open`. If your shell
cannot find `zhunt`, run `uv tool update-shell` and open a new terminal.

Alternatively, use Python's venv and pip from any directory:

```sh
python3.12 -m venv ~/.venvs/zhunt
~/.venvs/zhunt/bin/python -m pip install git+https://github.com/loopeywho/zhunt
~/.venvs/zhunt/bin/zhunt serve
```

Then run `~/.venvs/zhunt/bin/zhunt open` in another terminal. This installs the
application without a repository checkout or development dependencies.

## Your first engineering task

1. Open **Engineering** in the top bar and add an existing local project folder.
2. Create a named task. For **Isolated Git worktree**, the project must be a Git
   repository with a commit; only committed HEAD is copied to the new worktree.
3. Choose **Shell** and launch. Run your project's normal build or test command.
4. Use **+ Window → Engineering** for another terminal, and choose floating or
   tiled mode in the top bar. Closing a window keeps the terminal running.
5. Use **Stop session** to terminate the command and its descendants (a program that detached itself on purpose can survive). Stopping the
   daemon stops every terminal; project/task definitions persist, processes do not.

Terminals run local commands with your user permissions. Keep the daemon local;
this is not an OS sandbox or a multi-user remote terminal service. Terminals are
disabled automatically if you start the daemon with `--allow-non-loopback` on a
non-localhost `--host`.

## Coding agents and model routing

Install Codex, Hermes or Claude Code separately following that tool's official
instructions. Its executable must be on the PATH used to start `zhunt serve`.
Restart the daemon after changing PATH.

- **Codex · Zhunt routing** and **Hermes · Zhunt routing** use Zhunt's provider
  selection, quotas and failover. Watch **Routing**, **Providers** and **Logs**
  while the task runs. Model/tool compatibility varies; inspect code and run tests.
- **Codex · own account** and **Claude Code · own account** use the CLI's own
  authentication and billing. Zhunt's free routing and spending cap do not apply.
  Existing native approval controls remain enabled.

Use **Connect** or `zhunt keys add` for provider credentials. Keyless access is
subject to provider availability and shared quotas; free capacity is not guaranteed.
Paid routing remains opt-in. Run `zhunt doctor` to inspect local configuration.

The [validation record](ENGINEERING_VALIDATION.md) documents a real coding task,
observed free-model failover, usage accounting and the limits of those checks.
Own-account paid inference was not part of that validation.

## Update

Stop the daemon after saving terminal work, then run:

```sh
uv tool upgrade zhunt
zhunt serve
```

For the pip installation, instead run:

```sh
~/.venvs/zhunt/bin/python -m pip install --upgrade git+https://github.com/loopeywho/zhunt
```

Restart the daemon and refresh the workspace. Configuration lives in `~/.zhunt`;
keep credentials private. Older downloadable native installers do not contain
this Engineering release and must not be presented as tested or signed builds.

## Development checkout

```sh
git clone https://github.com/loopeywho/zhunt.git
cd zhunt
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
```
