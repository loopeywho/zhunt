"""Zhunt command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer

from zhunt.connect_cli import register as register_connect_commands
from zhunt.installer import Installer, InstallerError
from zhunt.keys_cli import keys_app
from zhunt.ports import resolve_daemon_port


app = typer.Typer(no_args_is_help=True)
register_connect_commands(app)
app.add_typer(keys_app, name="keys")


@app.callback()
def main() -> None:
    """Send each prompt to the best free-tier model, failing over automatically."""


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        help="Address to bind. Defaults to localhost only.",
    ),
    port: int | None = typer.Option(None, min=1, max=65_535),
    allow_non_loopback: bool = typer.Option(
        False,
        "--allow-non-loopback",
        help="Explicitly allow binding beyond localhost.",
    ),
    telemetry: Path = typer.Option(
        Path.home() / ".zhunt" / "telemetry.jsonl",
        "--telemetry",
        exists=False,
        dir_okay=False,
        help="Local JSONL telemetry path.",
    ),
    catalog: Path | None = typer.Option(
        None,
        "--catalog",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Provider catalog YAML; defaults to the packaged catalog.",
    ),
) -> None:
    """Run the local routing daemon and workspace."""

    selected_port, fell_back = resolve_daemon_port(
        requested=port,
        host=host,
        persist=False,
    )
    if fell_back:
        typer.echo(
            f"Port {port or 4000} is in use; using localhost:{selected_port} instead."
        )
        typer.echo(
            "Apps started with `zhunt launch` and `zhunt open` pick up the saved "
            "port; run `zhunt doctor` to check any app configured by an older Zhunt."
        )
    run_proxy(
        host=host,
        port=selected_port,
        allow_non_loopback=allow_non_loopback,
        telemetry_path=telemetry,
        catalog_path=catalog,
    )


@app.command("uninstall")
def uninstall_app(
    app_name: str | None = typer.Argument(
        None,
        help="App recipe to restore, or omit when using --all.",
    ),
    all_apps: bool = typer.Option(
        False,
        "--all",
        help="Restore every app configuration managed by Zhunt.",
    ),
) -> None:
    """Restore app configuration saved by an older Zhunt's `install`."""

    try:
        installer = create_installer()
        if all_apps:
            if app_name is not None:
                raise InstallerError("do not provide an app name with --all")
            apps = installer.installed_apps()
            if not apps:
                typer.echo("No Zhunt-managed app configurations found.")
                return
            for installed_app in apps:
                result = installer.uninstall(installed_app)
                typer.echo(f"Restored {installed_app}: {result.target}")
            return
        if app_name is None:
            raise InstallerError("provide an app name or use --all")
        result = installer.uninstall(app_name)
    except InstallerError as error:
        raise typer.BadParameter(str(error), param_hint="app_name") from error
    if result.manual:
        typer.echo(f"Manual removal required for {result.app}:")
        for instruction in result.manual_instructions:
            typer.echo(f"- {instruction}")
        return
    typer.echo(f"Restored {result.app}: {result.target}")


def run_proxy(
    *,
    host: str,
    port: int,
    allow_non_loopback: bool = False,
    telemetry_path: Path | None = None,
    catalog_path: Path | None = None,
) -> None:
    from zhunt.server import run_proxy as start_proxy

    start_proxy(
        host=host,
        port=port,
        allow_non_loopback=allow_non_loopback,
        telemetry_path=telemetry_path,
        catalog_path=catalog_path,
    )


def create_installer() -> Installer:
    return Installer()
