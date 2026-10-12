"""`zhunt catalog`: which free-model catalog is in use, and a manual update."""

from __future__ import annotations

import time

import typer

from zhunt import catalog_feed as feed
from zhunt import licence as lic

catalog_app = typer.Typer(
    help="The free-model catalog: shipped with zhunt, or a newer signed one you fetched.",
    no_args_is_help=True,
)


def _day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"


@catalog_app.command("status")
def status() -> None:
    """Show the catalog in use and whether updates are on. Nothing is fetched."""

    active = feed.load_active()
    if active.source == "feed":
        typer.echo(
            f"Catalog: version {active.version}, signed {active.channel} feed of {_day(active.issued_at)}"
            f" (valid until {_day(active.expires_at)})"
        )
    else:
        typer.echo(f"Catalog: version {active.version}, shipped with this zhunt")
    typer.echo(f"  {len(active.catalog.models)} models from {len(active.catalog.providers)} providers")
    if active.note:
        typer.echo(f"  Note: {active.note}")
    plan = lic.current_plan()
    channel = feed.channel_for(plan)
    if plan.has("catalog_live"):
        state = "on" if feed.updates_switched_on() else "off (switch it on in the workspace Connect window)"
        typer.echo(f"Automatic updates: {state}")
    else:
        typer.echo("Automatic updates: zhunt Pro only")
    typer.echo(f"`zhunt catalog update` fetches: {feed.feed_url(channel)}")


@catalog_app.command("update")
def update() -> None:
    """Fetch the signed catalog once (Pro: latest; Free: monthly snapshot) and use it if newer.

    One GET of a public file, with no key, licence or other identifier attached.
    """

    channel = feed.channel_for(lic.current_plan())
    typer.echo(f"Fetching {feed.feed_url(channel)}")
    result = feed.update(channel)
    if result.status == "error":
        typer.echo(f"Not updated: {result.message}", err=True)
        raise typer.Exit(1)
    typer.echo(result.message[:1].upper() + result.message[1:])
    if result.status == "applied":
        typer.echo("Saved to " + str(feed.cache_path()) + ". A running zhunt switches to it within a minute.")
