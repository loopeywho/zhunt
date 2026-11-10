"""``zhunt local``: check, set up, start and stop a model on this computer."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import typer

from zhunt import local_provider, local_setup
from zhunt.auth import read_env_values, save_env_value

local_app = typer.Typer(
    no_args_is_help=True,
    help="Run a model on this computer (Zhunt Plus) for private chats.",
)

ZHUNT_PLUS_ENV = "ZHUNT_PLUS_HOME"
DEFAULT_MODEL_DIRS = (Path.home() / ".local" / "share" / "inferssd" / "models",)
LICENCE_NOTE = (
    "Zhunt does not check model licences: make sure yours allows how you use it."
)


def _env_path() -> Path:
    return Path.home() / ".zhunt" / "env"


def _say_hardware(hw: local_setup.Hardware) -> bool:
    typer.echo(
        f"Apple Silicon: {'yes' if hw.apple_silicon else 'no'}; RAM: {hw.ram_gib} GiB; "
        f"free disk at {hw.disk_path}: {hw.free_disk_gb} GB"
    )
    errors, warnings = local_setup.hardware_problems(hw)
    for warning in warnings:
        typer.echo(f"Warning: {warning}")
    for error in errors:
        typer.echo(f"Problem: {error}", err=True)
    return not errors


def _pick_model(model: Path | None, model_dirs: list[Path]) -> Path:
    if model is not None:
        return local_setup.complete_model(model)
    found = local_setup.find_models(model_dirs or DEFAULT_MODEL_DIRS)
    if not found:
        raise local_setup.SetupError(
            "no complete .gguf model found; pass --model or --model-dir "
            "(nothing is downloaded)"
        )
    for path in found:
        typer.echo(f"Found model: {path} ({local_setup.model_bytes(path) / 1e9:.1f} GB)")
    return found[0]


def _bench(model: Path) -> None:
    typer.echo(f"Timing random SSD reads of {model.name} (read-only, about 6 seconds)...")
    typer.echo(f"SSD random read: {local_setup.ssd_read_benchmark(model)} GB/s")


def _confirm(question: str, yes: bool) -> None:
    if not yes and not typer.confirm(question, default=False):
        raise typer.Exit(1)


def _wait_until_answering(base_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if asyncio.run(local_provider.is_answering(base_url)):
            return True
        time.sleep(3)
    return False


def _register(settings: local_setup.ServerSettings) -> None:
    save_env_value(local_provider.URL_ENV, settings.base_url, _env_path())
    save_env_value(local_provider.CONTEXT_ENV, str(settings.context), _env_path())
    typer.echo(
        f"Registered the local model at {settings.base_url} in {_env_path()}. "
        "Restart the Zhunt daemon to use it; mark a chat private with the "
        "`zhunt-local` model or the `x-zhunt-private: 1` header."
    )


@local_app.command("check")
def check(
    model: Path | None = typer.Option(None, help="A .gguf model (or its first shard)."),
    model_dir: list[Path] = typer.Option([], help="Folder to look for models in."),
    bench: bool = typer.Option(True, help="Time SSD reads of the model found."),
) -> None:
    """Check this computer and look for a model already on disk. Changes nothing."""

    try:
        picked = _pick_model(model, model_dir)
    except local_setup.SetupError as error:
        picked = None
        typer.echo(str(error))
    ok = _say_hardware(local_setup.hardware(picked.parent if picked else Path.home()))
    if picked is not None and bench:
        _bench(picked)
    typer.echo(LICENCE_NOTE)
    if not ok:
        raise typer.Exit(1)


@local_app.command("setup")
def setup(
    zhunt_plus: Path | None = typer.Option(
        None, envvar=ZHUNT_PLUS_ENV,
        help="Zhunt Plus folder (patches and launcher); or set ZHUNT_PLUS_HOME.",
    ),
    engine_source: Path | None = typer.Option(
        None, help="Local InferSSD git checkout. Default: <zhunt-plus>/engines/inferssd.",
    ),
    engine_dir: Path = typer.Option(
        local_setup.STATE_DIR / "inferssd", help="Where to build the patched engine.",
    ),
    model: Path | None = typer.Option(None, help="A .gguf model (or its first shard)."),
    model_dir: list[Path] = typer.Option([], help="Folder to look for models in."),
    port: int = typer.Option(local_provider.DEFAULT_PORT, min=1, max=65_535),
    ram: int = typer.Option(11, min=1, help="Memory budget for the engine, GiB."),
    context: int = typer.Option(local_provider.DEFAULT_CONTEXT, min=1),
    python: str | None = typer.Option(None, help="Python 3.9+ for the engine scripts."),
    bench: bool = typer.Option(True, help="Time SSD reads of the model."),
    start: bool = typer.Option(True, help="Start the server and register it when done."),
    wait: float = typer.Option(900, help="Seconds to wait for the model to load."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask before building or loading."),
) -> None:
    """Check hardware, build the patched engine, find a model, start it and register it."""

    try:
        if zhunt_plus is None:
            raise local_setup.SetupError(
                f"pass --zhunt-plus or set {ZHUNT_PLUS_ENV} to the Zhunt Plus folder"
            )
        zhunt_plus = zhunt_plus.expanduser()
        source = (engine_source or zhunt_plus / "engines" / "inferssd").expanduser()
        launcher = zhunt_plus / "engine" / "inferssd_engine.py"
        if not launcher.is_file():
            raise local_setup.SetupError(f"no launcher at {launcher}")
        python = python or shutil.which("python3")
        if not python:
            raise local_setup.SetupError("python3 not found; pass --python")
        patches = local_setup.patch_files(zhunt_plus)

        typer.echo("1/5 Hardware")
        picked = _pick_model(model, model_dir)
        if not _say_hardware(local_setup.hardware(engine_dir)):
            raise typer.Exit(1)

        typer.echo("2/5 SSD")
        if bench:
            _bench(picked)

        typer.echo("3/5 Engine")
        engine_dir = engine_dir.expanduser()
        if local_setup.engine_is_built(engine_dir, patches):
            typer.echo(f"Already built: {engine_dir}")
        else:
            _confirm(
                f"Build InferSSD {local_setup.PINNED_REVISION[:7]} with "
                f"{len(patches)} patches in {engine_dir}? (about 2 to 12 minutes; "
                f"it should need no downloads, which a real build has not yet confirmed)",
                yes,
            )
            local_setup.prepare_engine(
                source=source, engine_dir=engine_dir, patches=patches,
                python=python, log=typer.echo,
            )

        typer.echo("4/5 Model")
        typer.echo(f"Using {picked}")
        typer.echo(LICENCE_NOTE)
        settings = local_setup.ServerSettings(
            launcher=str(launcher), python=python, engine_dir=str(engine_dir),
            model=str(picked), port=port, ram_gib=ram, context=context,
        )
        local_setup.save_settings(settings)

        typer.echo("5/5 Server")
        if not start:
            typer.echo("Not started (--no-start). Run `zhunt local start` later.")
            return
        _start_and_register(settings, yes=yes, wait=wait)
    except (local_setup.SetupError, subprocess.CalledProcessError, OSError) as error:
        typer.echo(f"Setup stopped: {error}", err=True)
        raise typer.Exit(1) from error


def _start_and_register(
    settings: local_setup.ServerSettings, *, yes: bool, wait: float
) -> None:
    _confirm(
        f"Load {Path(settings.model).name} on port {settings.port} "
        f"(about {settings.ram_gib} GiB of memory and heavy SSD reads)?",
        yes,
    )
    pid = local_setup.start_server(settings)
    typer.echo(f"Started (pid {pid}); log: {local_setup.STATE_DIR / 'server.log'}")
    if not _wait_until_answering(settings.base_url, wait):
        typer.echo(
            f"The server did not answer at {settings.base_url} within {wait:.0f} s; "
            "check the log. Not registered.",
            err=True,
        )
        raise typer.Exit(1)
    _register(settings)


@local_app.command("start")
def start_command(
    yes: bool = typer.Option(False, "--yes", help="Do not ask before loading."),
    wait: float = typer.Option(900, help="Seconds to wait for the model to load."),
) -> None:
    """Start the local model set up earlier and register it."""

    try:
        _start_and_register(local_setup.load_settings(), yes=yes, wait=wait)
    except (local_setup.SetupError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


@local_app.command("stop")
def stop_command() -> None:
    """Stop the local model server started by `zhunt local`."""

    typer.echo("Stopped." if local_setup.stop_server() else "It was not running.")


@local_app.command("status")
def status_command() -> None:
    """Show the registered local model and whether it is answering."""

    try:
        config = local_provider.config_from_env({**read_env_values(_env_path()), **os.environ})
    except local_provider.LocalConfigError as error:
        typer.echo(f"Local model settings are wrong: {error}")
        raise typer.Exit(1) from error
    if config is None:
        typer.echo("No local model registered (run `zhunt local setup`).")
        return
    answering = asyncio.run(local_provider.is_answering(config.base_url))
    typer.echo(
        f"Local model: {config.base_url} (context {config.context_window} tokens): "
        f"{'answering' if answering else 'not answering'}"
    )
