"""Run another app with the Zhunt endpoint injected into that process only.

Nothing here writes a file. The endpoint and key reach the child through its
environment and, where the app documents one, command-line config overrides
(which never contain the key). Each mechanism below was checked against the
app's own help output or documentation; see the notes on each builder.
"""

from __future__ import annotations

import http.client
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Alias the daemon always serves; routing picks the real model per request.
MODEL_ALIAS = "zhunt-auto"
# Name of the env var that carries the key to Codex (the name, not the value,
# is what appears on its command line).
CODEX_KEY_ENV = "ZHUNT_API_KEY"
REDACTED = "***"

LAUNCHABLE_APPS = ("codex", "hermes")

# Apps that can only be pointed at Zhunt through their own settings UI/files.
MANUAL_APPS: dict[str, tuple[str, ...]] = {
    "cursor": (
        "Cursor has no environment variable or command-line route for a custom endpoint.",
        "Open Cursor Settings, then Models, and enable Override OpenAI Base URL.",
        "Enter http://127.0.0.1:{port}/v1 as the base URL.",
        "Run `zhunt open --print` and copy the key after `#key=` (it is also "
        "ZHUNT_MASTER_KEY in ~/.zhunt/env). Enter it as the API key.",
        "Add the model zhunt-auto and select it for Chat and Agent.",
    ),
    "vscode": (
        "VS Code has no environment variable or command-line route for a custom endpoint.",
        "No install step is needed: run `zhunt launch vscode`, or add the entry by hand "
        "in Chat > Manage Language Models with endpoint "
        "http://127.0.0.1:{port}/v1/chat/completions and model zhunt-auto.",
        "The key is ZHUNT_MASTER_KEY in ~/.zhunt/env.",
    ),
}

class LaunchError(RuntimeError):
    """A launch cannot proceed; the message is safe to show."""


@dataclass(frozen=True)
class LaunchPlan:
    app: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    secret_names: frozenset[str]
    warnings: tuple[str, ...] = field(default=())

    def render(self, key: str) -> str:
        """Human-readable plan; every secret value is shown as ***."""

        lines = ["Would run: " + " ".join(_shell_quote(part) for part in self.argv)]
        lines.append("With environment (this process only):")
        for name in sorted(self.env):
            value = REDACTED if name in self.secret_names else self.env[name]
            lines.append(f"  {name}={value}")
        text = "\n".join(lines)
        # Defence in depth: whatever happens above, the key never prints.
        return text.replace(key, REDACTED) if key else text


def build_plan(
    app: str,
    *,
    port: int,
    key: str,
    extra_args: Sequence[str] = (),
) -> LaunchPlan:
    if app not in LAUNCHABLE_APPS:
        raise LaunchError(f"unknown app: {app}")
    root = f"http://127.0.0.1:{port}"
    builders = {"codex": _codex, "hermes": _hermes}
    return builders[app](root, key, tuple(extra_args))


def _codex(root: str, key: str, extra: tuple[str, ...]) -> LaunchPlan:
    # Evidence: Codex config docs (learn.chatgpt.com/docs/config-file/config-advanced):
    # `-c key=value` overrides one run without editing config.toml, values are TOML,
    # dotted keys set nested tables; [model_providers.<id>] takes base_url, env_key
    # and wire_api = "responses" (the only supported value). `env_key` names the
    # variable holding the key, so the key itself stays off the command line.
    # Not exercised live: the codex binary is not installed on the build machine.
    overrides = (
        'model_provider="zhunt"',
        f'model="{MODEL_ALIAS}"',
        'model_providers.zhunt.name="Zhunt"',
        f'model_providers.zhunt.base_url="{root}/v1"',
        f'model_providers.zhunt.env_key="{CODEX_KEY_ENV}"',
        'model_providers.zhunt.wire_api="responses"',
    )
    argv = ["codex"]
    for override in overrides:
        argv.extend(["-c", override])
    argv.extend(extra)
    return LaunchPlan(
        app="codex",
        argv=tuple(argv),
        env={CODEX_KEY_ENV: key},
        secret_names=frozenset({CODEX_KEY_ENV}),
    )


def _hermes(root: str, key: str, extra: tuple[str, ...]) -> LaunchPlan:
    # Evidence: `hermes --help` (--provider, -m), and Hermes' own runtime resolver
    # (hermes_cli/runtime_provider_backends.py), run offline against a throwaway
    # HERMES_HOME: with `--provider custom`, CUSTOM_BASE_URL picks the endpoint and
    # OPENAI_API_KEY is used only when OPENAI_BASE_URL equals that endpoint exactly.
    # The explicit --provider flag outranks config.yaml's model.provider.
    endpoint = f"{root}/v1"
    env = {
        "CUSTOM_BASE_URL": endpoint,
        "OPENAI_BASE_URL": endpoint,
        "OPENAI_API_KEY": key,
    }
    return LaunchPlan(
        app="hermes",
        argv=("hermes", "--provider", "custom", "-m", MODEL_ALIAS, *extra),
        env=env,
        secret_names=frozenset({"OPENAI_API_KEY"}),
    )


def preflight_warnings(
    app: str,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Read-only check for files that would override the injected environment.

    Only variable NAMES are inspected; values are never read into a message.
    """

    home = (home or Path.home()).expanduser()
    cwd = cwd or Path.cwd()
    environ = os.environ if environ is None else environ
    warnings: list[str] = []
    if app == "hermes":
        hermes_home = Path(environ.get("HERMES_HOME") or home / ".hermes").expanduser()
        found = _dotenv_names(
            hermes_home / ".env",
            {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CUSTOM_BASE_URL"},
        )
        if found:
            warnings.append(
                f"{hermes_home / '.env'} defines {', '.join(sorted(found))}; Hermes loads "
                "that file over the shell environment, so it will override the Zhunt "
                "endpoint or key. Remove those lines or launch with a different HERMES_HOME."
            )
    return tuple(warnings)


def daemon_reachable(port: int, *, timeout: float = 1.0) -> bool:
    """True if anything on loopback answers HTTP. No credential is sent."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/")
        connection.getresponse().read()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def run_plan(plan: LaunchPlan, *, environ: Mapping[str, str] | None = None) -> int:
    """Run the plan as a child process and return its exit status.

    stdio is inherited. SIGTERM/SIGHUP/SIGQUIT are forwarded; SIGINT is forwarded
    only without a terminal on stdin (a terminal already delivers Ctrl-C to the
    whole foreground group, and forwarding it again would double-interrupt).
    """

    child_env = dict(os.environ if environ is None else environ)
    child_env.update(plan.env)
    executable = shutil.which(plan.argv[0], path=child_env.get("PATH"))
    if executable is None:
        raise LaunchError(f"`{plan.argv[0]}` was not found on PATH; install it first.")

    child = subprocess.Popen([executable, *plan.argv[1:]], env=child_env)

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    forwarded = [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT]
    tty_stdin = _stdin_is_tty()
    handlers: dict[int, object] = {}
    try:
        for signum in forwarded:
            handlers[signum] = signal.signal(signum, forward)
        # A no-op handler, not SIG_IGN: ignored signals survive exec, which would
        # leave the child unable to be interrupted.
        handlers[signal.SIGINT] = signal.signal(
            signal.SIGINT, (lambda *_: None) if tty_stdin else forward
        )
    except ValueError:  # not the main thread; run without forwarding
        pass
    try:
        while True:
            try:
                status = child.wait()
                break
            except KeyboardInterrupt:
                continue
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)  # type: ignore[arg-type]
    return 128 - status if status < 0 else status


def _stdin_is_tty() -> bool:
    try:
        return os.isatty(sys.stdin.fileno())
    except (OSError, ValueError, AttributeError):
        return False


def _dotenv_names(path: Path, wanted: set[str]) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    found: set[str] = set()
    for line in lines:
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() in wanted and value.strip():
            found.add(name.strip())
    return found


def _shell_quote(part: str) -> str:
    return shlex.quote(part)
