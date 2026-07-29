"""Zhunt command-line interface."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

from zhunt.installer import InstallationResult, Installer, InstallerError


app = typer.Typer(no_args_is_help=True)


class InstallMode(str, Enum):
    API = "api"
    PASSTHROUGH = "passthrough"


@app.callback()
def main() -> None:
    """Route LLM requests to the cheapest capable model."""


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        help="Address to bind. Defaults to localhost only.",
    ),
    port: int = typer.Option(4000, min=1, max=65_535),
    registry: Path | None = typer.Option(
        None,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to models.yaml.",
    ),
    allow_non_loopback: bool = typer.Option(
        False,
        "--allow-non-loopback",
        help="Explicitly allow binding beyond localhost.",
    ),
) -> None:
    """Run the local LiteLLM-backed routing daemon."""

    run_proxy(
        host=host,
        port=port,
        registry_path=registry,
        allow_non_loopback=allow_non_loopback,
    )


@app.command("install")
def install_app(
    app_name: str = typer.Argument(help="App recipe to install."),
    mode: InstallMode = typer.Option(
        InstallMode.PASSTHROUGH,
        help="API billing mode or registration-only passthrough mode.",
    ),
    base_url: str = typer.Option(
        "http://127.0.0.1:4000",
        help="Zhunt daemon base URL.",
    ),
) -> None:
    """Back up and configure an app to use Zhunt."""

    try:
        result = create_installer().install(
            app_name,
            mode=mode.value,
            base_url=base_url,
        )
    except InstallerError as error:
        raise typer.BadParameter(str(error), param_hint="app_name") from error
    _show_install_result(result)


@app.command("uninstall")
def uninstall_app(
    app_name: str = typer.Argument(help="App recipe to restore."),
) -> None:
    """Restore the configuration saved before Zhunt installation."""

    try:
        result = create_installer().uninstall(app_name)
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
    registry_path: Path | None,
    allow_non_loopback: bool = False,
) -> None:
    from zhunt.server import run_proxy as start_proxy

    start_proxy(
        host=host,
        port=port,
        registry_path=registry_path,
        allow_non_loopback=allow_non_loopback,
    )


def create_installer() -> Installer:
    return Installer()


def _show_install_result(result: InstallationResult) -> None:
    if result.manual:
        typer.echo(f"Manual setup required for {result.app}:")
        for instruction in result.manual_instructions:
            typer.echo(f"- {instruction}")
        return
    typer.echo(f"Configured {result.app}: {result.target}")
    if result.backup is not None:
        typer.echo(f"Original backup: {result.backup}")
