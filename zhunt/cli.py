"""Zhunt command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer


app = typer.Typer(no_args_is_help=True)


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
) -> None:
    """Run the local LiteLLM-backed routing daemon."""

    run_proxy(host=host, port=port, registry_path=registry)


def run_proxy(
    *,
    host: str,
    port: int,
    registry_path: Path | None,
) -> None:
    from zhunt.server import run_proxy as start_proxy

    start_proxy(host=host, port=port, registry_path=registry_path)
