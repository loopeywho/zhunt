"""`zhunt keys`: store provider API keys without ever displaying them."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from zhunt.auth import read_env_values, save_env_value
from zhunt.catalog import Catalog, Provider, load_catalog

keys_app = typer.Typer(
    help="Add, list or remove provider API keys (stored in ~/.zhunt/env, mode 0600).",
    no_args_is_help=True,
)


def _env_path() -> Path:
    return Path.home() / ".zhunt" / "env"


def _provider(catalog: Catalog, platform: str) -> Provider:
    provider = catalog.providers.get(platform)
    if provider is None:
        known = ", ".join(sorted(catalog.providers))
        raise typer.BadParameter(
            f"unknown provider {platform!r}; choose one of: {known}",
            param_hint="platform",
        )
    return provider


@keys_app.command("list")
def list_keys() -> None:
    """Show which providers have a key set. Values are never printed."""

    stored = read_env_values(_env_path())
    for provider in load_catalog().providers.values():
        is_set = bool(stored.get(provider.key_env) or os.environ.get(provider.key_env))
        typer.echo(
            f"{provider.platform:<10} {provider.key_env:<18} "
            f"{'set' if is_set else 'not set'}"
        )


@keys_app.command("add")
def add_key(platform: str = typer.Argument(help="Provider, e.g. groq.")) -> None:
    """Save a provider key. It is read from a hidden prompt, never from an argument."""

    provider = _provider(load_catalog(), platform)
    value = typer.prompt(f"{provider.name} API key", hide_input=True).strip()
    if not value:
        raise typer.BadParameter("the key is empty", param_hint="key")
    try:
        save_env_value(provider.key_env, value, _env_path())
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="key") from error
    typer.echo(f"Saved {provider.key_env} to {_env_path()}")


@keys_app.command("remove")
def remove_key(platform: str = typer.Argument(help="Provider, e.g. groq.")) -> None:
    """Delete a provider key from ~/.zhunt/env."""

    provider = _provider(load_catalog(), platform)
    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    kept = [line for line in lines if line.partition("=")[0] != provider.key_env]
    if len(kept) == len(lines):
        typer.echo(f"{provider.key_env} was not set in {path}")
        return
    path.write_text("\n".join(kept).rstrip("\n") + "\n" if kept else "", encoding="utf-8")
    path.chmod(0o600)
    typer.echo(f"Removed {provider.key_env} from {path}")
