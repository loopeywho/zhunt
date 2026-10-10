"""`zhunt launch`, `zhunt doctor` and `zhunt open`: connect other apps to Zhunt."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from urllib.parse import quote

import typer

from zhunt.auth import read_master_key
from zhunt.doctor import has_failure, run_checks
from zhunt.launch import (
    LAUNCHABLE_APPS,
    MANUAL_APPS,
    LaunchError,
    build_plan,
    daemon_reachable,
    preflight_warnings,
    run_plan,
)
from zhunt.ports import DEFAULT_DAEMON_PORT, configured_port

_PORT_HELP = "Daemon port; defaults to the saved port, then 4000."
EXIT_FAILURE = 1
EXIT_MANUAL = 2
EXIT_NOT_FOUND = 127


def _port(port: int | None) -> int:
    return port or configured_port() or DEFAULT_DAEMON_PORT


def launch(
    ctx: typer.Context,
    app_name: str = typer.Argument(
        help=f"App to start: {', '.join(LAUNCHABLE_APPS)}. Arguments after `--` go to the app.",
    ),
    port: int | None = typer.Option(None, min=1, max=65_535, help=_PORT_HELP),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would run, with the key shown as ***, and start nothing.",
    ),
) -> None:
    """Run an app with Zhunt as its endpoint, for that process only.

    The endpoint and key are passed through the child's environment (and, for
    Codex, `-c` config overrides that name an env var, never the key). No config
    file is written, so app updates cannot break the connection. Exit status and
    stdio are the app's own; TERM/HUP/QUIT are forwarded.

    Exit codes: 1 daemon unreachable or no key, 2 no config-free route (manual
    steps printed), 127 app not installed.
    """

    selected_port = _port(port)
    if app_name in MANUAL_APPS:
        typer.echo(f"{app_name} cannot be launched with an injected endpoint. Manual steps:")
        for step in MANUAL_APPS[app_name]:
            typer.echo(f"- {step.format(port=selected_port)}")
        raise typer.Exit(EXIT_MANUAL)
    if app_name not in LAUNCHABLE_APPS:
        known = ", ".join((*LAUNCHABLE_APPS, *MANUAL_APPS))
        raise typer.BadParameter(
            f"unknown app {app_name!r}; choose from {known}", param_hint="app_name"
        )

    key = read_master_key()
    if key is None and not dry_run:
        typer.echo(
            "No Zhunt master key found in ~/.zhunt/env. Start the daemon once with "
            "`zhunt serve --free-tier`; it creates the key.",
            err=True,
        )
        raise typer.Exit(EXIT_FAILURE)
    plan = build_plan(app_name, port=selected_port, key=key or "", extra_args=ctx.args)
    reachable = daemon_reachable(selected_port)
    if dry_run:
        typer.echo(plan.render(key or ""))
        state = "reachable" if reachable else "NOT reachable"
        typer.echo(f"Daemon on 127.0.0.1:{selected_port}: {state}")
        for warning in preflight_warnings(app_name):
            typer.echo(f"warning: {warning}")
        return
    if not reachable:
        typer.echo(
            f"The Zhunt daemon is not answering on 127.0.0.1:{selected_port}. "
            "Start it with `zhunt serve --free-tier`, then run this again.",
            err=True,
        )
        raise typer.Exit(EXIT_FAILURE)
    for warning in preflight_warnings(app_name):
        typer.echo(f"warning: {warning}", err=True)
    try:
        status = run_plan(plan)
    except LaunchError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(EXIT_NOT_FOUND) from error
    raise typer.Exit(status)


def doctor(
    port: int | None = typer.Option(None, min=1, max=65_535, help=_PORT_HELP),
    catalog: Path | None = typer.Option(
        None,
        "--catalog",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Catalog YAML to check; defaults to the packaged catalog.",
    ),
) -> None:
    """Check the daemon, keys, policy flags, catalog age and v1 app configs.

    Prints OK, WARN or FAIL per check with a one-line fix. Provider key NAMES
    are reported, never values. Exits non-zero if any check FAILs. App drift
    is a WARN: v1 config files can disagree with the current port or key, and
    `zhunt launch` does not depend on them.
    """

    checks = run_checks(home=Path.home(), port=_port(port), catalog_path=catalog)
    for check in checks:
        typer.echo(check.render())
    failures = sum(1 for check in checks if check.status.value == "FAIL")
    warnings = sum(1 for check in checks if check.status.value == "WARN")
    typer.echo(f"{len(checks)} checks: {failures} failed, {warnings} warnings")
    if has_failure(checks):
        raise typer.Exit(EXIT_FAILURE)


def open_workspace(
    print_only: bool = typer.Option(
        False,
        "--print",
        help="Print the URL (it contains the key) instead of opening a browser.",
    ),
    port: int | None = typer.Option(None, min=1, max=65_535, help=_PORT_HELP),
) -> None:
    """Open the workspace in your browser, already signed in.

    The URL is http://127.0.0.1:PORT/workspace/#key=KEY. The key sits in the URL
    fragment, which browsers never send to a server and servers therefore cannot
    log; the page reads it locally. Treat the printed URL as a secret.
    """

    selected_port = _port(port)
    key = read_master_key()
    if key is None:
        typer.echo(
            "No Zhunt master key found in ~/.zhunt/env. Start the daemon once with "
            "`zhunt serve --free-tier`; it creates the key.",
            err=True,
        )
        raise typer.Exit(EXIT_FAILURE)
    if not daemon_reachable(selected_port):
        typer.echo(
            f"warning: nothing is answering on 127.0.0.1:{selected_port}; "
            "start the daemon with `zhunt serve --free-tier`.",
            err=True,
        )
    url = f"http://127.0.0.1:{selected_port}/workspace/#key={quote(key, safe='')}"
    if print_only:
        typer.echo(url)
        return
    if not webbrowser.open(url):
        typer.echo(
            "Could not open a browser; run `zhunt open --print` and open the URL yourself.",
            err=True,
        )
        raise typer.Exit(EXIT_FAILURE)
    typer.echo(
        f"Opened http://127.0.0.1:{selected_port}/workspace/ (key sent in the URL fragment only)"
    )


def register(app: typer.Typer) -> None:
    app.command(
        "launch",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(launch)
    app.command("doctor")(doctor)
    app.command("open")(open_workspace)
