"""`zhunt licence`: activate, check, renew or remove a Pro licence key."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import typer

from zhunt import licence as lic
from zhunt.auth import read_env_values, save_env_value
from zhunt.spend import DEFAULT_MONTHLY_CAP, MONTHLY_CAP_ENV

licence_app = typer.Typer(help="Manage your zhunt Pro licence (stored in ~/.zhunt/licence, mode 0600).", no_args_is_help=True)

RENEW_URL = "https://zhunt.kindredwildcat.com/api/licence"


def _when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"


def _describe(plan: lic.Plan) -> None:
    if plan.name == "free":
        typer.echo("Plan: Free (free-tier models, dark and light themes)")
        if plan.problem:
            typer.echo(f"A stored licence key is not in effect: {plan.problem}")
        typer.echo(f"Upgrade: zhunt upgrade  ({lic.UPGRADE_URL})")
        return
    typer.echo(f"Plan: {plan.name.title()}" + (f" for {plan.email}" if plan.email else ""))
    typer.echo(f"Renews by: {_when(plan.expires_at)}" + ("  (expired; in the 7-day grace period, run `zhunt licence renew`)" if plan.in_grace else ""))
    for feature in sorted(plan.features):
        typer.echo(f"  + {lic.FEATURE_LABELS.get(feature, feature)}")


@licence_app.command("status")
def status() -> None:
    """Show the plan in effect and what it includes."""

    _describe(lic.current_plan())


@licence_app.command("activate")
def activate(key: str = typer.Argument(None, help="The licence key; omit to paste it at a prompt.")) -> None:
    """Check and store a licence key. Nothing is sent anywhere: keys are verified offline."""

    if not key:
        key = sys.stdin.readline() if not sys.stdin.isatty() else typer.prompt("Licence key")
    try:
        plan = lic.save_key(key)
    except lic.LicenceError as error:
        typer.echo(f"Not activated: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"Activated. Saved to {lic.licence_path()}")
    _describe(plan)
    typer.echo("Restart `zhunt serve` (or reload the workspace) for it to take effect.")


@licence_app.command("renew")
def renew() -> None:
    """Fetch a fresh key for an active subscription (the only command that contacts the zhunt website)."""

    key = lic.stored_key()
    sub = lic.subscription_of(key) if key else None
    plan = lic.current_plan()
    if not key or not sub:
        typer.echo("No renewable licence is stored. Buy or restart a subscription at " + lic.UPGRADE_URL, err=True)
        raise typer.Exit(1)
    query = urllib.parse.urlencode({"renew": sub, "email": plan.email or ""})
    try:
        with urllib.request.urlopen(f"{RENEW_URL}?{query}", timeout=20) as response:  # noqa: S310 (fixed https URL)
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        typer.echo(f"Renewal refused ({error.code}): {error.read().decode(errors='replace')[:300]}", err=True)
        raise typer.Exit(1) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        typer.echo(f"Could not reach the licence service: {error}", err=True)
        raise typer.Exit(1) from error
    try:
        new_plan = lic.save_key(str(body.get("licence", "")))
    except lic.LicenceError as error:
        typer.echo(f"The licence service returned an unusable key: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Renewed.")
    _describe(new_plan)


@licence_app.command("remove")
def remove() -> None:
    """Delete the stored licence key (back to Free)."""

    typer.echo("Removed." if lic.remove_key() else "No licence key was stored.")


def upgrade() -> None:
    """Open the pricing page in your browser."""

    typer.echo(f"zhunt Pro: {lic.UPGRADE_URL}")
    for feature in sorted(lic.FEATURES["pro"]):
        typer.echo(f"  + {lic.FEATURE_LABELS[feature]}")
    typer.echo("After checkout, run: zhunt licence activate")
    webbrowser.open(lic.UPGRADE_URL)


premium_app = typer.Typer(
    help="Pro: use premium models (via your OpenRouter key) when every free model is busy, under a monthly cap.",
    no_args_is_help=True,
)


def _env_file() -> Path:
    return Path.home() / ".zhunt" / "env"


@premium_app.command("on")
def premium_on(
    cap: float = typer.Option(DEFAULT_MONTHLY_CAP, help="Most you allow per month, in USD (provider prices)."),
) -> None:
    """Allow premium models up to CAP dollars a month. Needs Pro and an OpenRouter key."""

    if cap <= 0:
        raise typer.BadParameter("the cap must be more than 0", param_hint="--cap")
    save_env_value("ZHUNT_ALLOW_PAID", "1", _env_file())
    save_env_value(MONTHLY_CAP_ENV, f"{cap:g}", _env_file())
    typer.echo(f"Premium fallback on, capped at ${cap:.2f} a month. Restart `zhunt serve` to apply.")
    if not lic.current_plan().has("premium_fallback"):
        typer.echo("Note: this needs zhunt Pro. See `zhunt upgrade`.")
    if not read_env_values(_env_file()).get("OPENROUTER_API_KEY"):
        typer.echo("Add your paid OpenRouter key: zhunt keys add openrouter")


@premium_app.command("off")
def premium_off() -> None:
    """Never use premium models (free models only)."""

    save_env_value("ZHUNT_ALLOW_PAID", "0", _env_file())
    typer.echo("Premium fallback off. Restart `zhunt serve` to apply.")


@premium_app.command("status")
def premium_status() -> None:
    """Show whether premium fallback can run, and the cap."""

    env = read_env_values(_env_file())
    plan = lic.current_plan()
    opted = env.get("ZHUNT_ALLOW_PAID", "").lower() in {"1", "true", "yes", "on"}
    typer.echo(f"Plan:     {plan.name}{'' if plan.has('premium_fallback') else '  (premium needs Pro: zhunt upgrade)'}")
    typer.echo(f"Opted in: {'yes' if opted else 'no (zhunt premium on)'}")
    typer.echo(f"Cap:      ${float(env.get(MONTHLY_CAP_ENV) or DEFAULT_MONTHLY_CAP):.2f} a month")
    typer.echo(f"Key:      {'OPENROUTER_API_KEY set' if env.get('OPENROUTER_API_KEY') else 'no OpenRouter key (zhunt keys add openrouter)'}")
    typer.echo("Spend so far this month is in the workspace (Panels and For you).")
