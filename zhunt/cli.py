"""Zhunt command-line interface."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import secrets
import webbrowser

import typer

from zhunt.benchmark import run_benchmark
from zhunt.installer import InstallationResult, Installer, InstallerError
from zhunt.onboarding import create_onboarding_app
from zhunt.pricing import PricingSyncError, sync_registry
from zhunt.registry import ModelRegistry
from zhunt.telemetry import summarize_telemetry


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


@app.command()
def setup(
    host: str = typer.Option(
        "127.0.0.1",
        help="Address for the local setup page. Non-loopback hosts are refused.",
    ),
    port: int = typer.Option(8400, min=1, max=65_535),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print the setup URL without opening a browser.",
    ),
) -> None:
    """Open the local provider and app setup page."""

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter(
            "setup is local-only; use 127.0.0.1, localhost, or ::1",
            param_hint="host",
        )
    token = secrets.token_urlsafe(24)
    url = f"http://{host}:{port}/?token={token}"
    typer.echo(f"Open Zhunt setup: {url}")
    if not no_browser:
        webbrowser.open(url)
    import uvicorn

    uvicorn.run(
        create_onboarding_app(setup_token=token),
        host=host,
        port=port,
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


@app.command()
def status(
    telemetry: Path = typer.Option(
        Path.home() / ".zhunt" / "telemetry.jsonl",
        exists=False,
        dir_okay=False,
        help="Local request telemetry JSONL path.",
    ),
) -> None:
    """Show today's local spend and counterfactual top-model spend."""

    summary = summarize_telemetry(telemetry)
    typer.echo(f"Date: {summary['day']}")
    typer.echo(f"Requests: {summary['requests']}")
    typer.echo(f"Actual spend: ${summary.get('actual_spend', 0.0):.6f}")
    typer.echo(
        "Counterfactual top-model spend: "
        f"${summary.get('counterfactual_spend', 0.0):.6f}"
    )
    typer.echo(f"Savings: ${summary.get('savings', 0.0):.6f}")
    for app_name, app_summary in sorted(summary["by_app"].items()):
        typer.echo(
            f"{app_name}: {app_summary['requests']} requests, "
            f"${app_summary['actual_spend']:.6f} actual"
        )


@app.command()
def benchmark(
    provider: str | None = typer.Option(
        None,
        help="Provider profile to benchmark, such as nous-portal.",
    ),
    registry: Path | None = typer.Option(
        None,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional models.yaml path; defaults to the packaged registry.",
    ),
    output: Path | None = typer.Option(
        None,
        help="Optional JSON output path.",
    ),
) -> None:
    """Run the offline routing benchmark without provider calls."""

    selected_registry = (
        ModelRegistry.from_path(registry, provider_id=provider)
        if registry is not None
        else ModelRegistry.default(provider_id=provider)
    )
    result = run_benchmark(selected_registry)
    if output is not None:
        import json

        output = output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        typer.echo(f"Wrote benchmark report: {output}")
    typer.echo(f"Requests: {result['requests']}")
    typer.echo(f"Projected Zhunt cost: ${result['actual_projected_cost']:.6f}")
    typer.echo(
        "Counterfactual top-model cost: "
        f"${result['baseline_projected_cost']:.6f}"
    )
    typer.echo(
        "Projected savings: "
        f"${result['projected_savings']:.6f} "
        f"({result['projected_savings_percent']:.1f}%)"
    )
    typer.echo("Quality measured: no (offline benchmark)")
    typer.echo("Provider calls: no")
    for turn in result["turns"]:
        typer.echo(
            f"- {turn['case']}/{turn['turn']}: "
            f"{turn['tier']} -> {turn['model']}"
        )


@app.command()
def sync(
    registry: Path = typer.Option(
        Path("models.yaml"),
        exists=True,
        dir_okay=False,
        readable=True,
        help="Registry file to refresh from OpenRouter.",
    ),
) -> None:
    """Refresh matching model prices from OpenRouter's models API."""

    try:
        result = sync_registry(registry)
    except PricingSyncError as error:
        raise typer.BadParameter(str(error), param_hint="registry") from error
    typer.echo(f"Updated: {len(result.updated)} models")
    typer.echo(f"Unavailable: {len(result.unavailable)} models")
    if result.cheaper_tiers:
        typer.echo("Cheaper tier candidates: " + ", ".join(result.cheaper_tiers))


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
