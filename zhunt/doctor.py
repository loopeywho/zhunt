"""Read-only health checks for a Zhunt install. Nothing here writes a file."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from zhunt.auth import read_env_values, read_master_key
from zhunt.catalog import Catalog
from zhunt.installer import supported_apps
from zhunt.installer.apply import (
    InstallerError,
    _load_recipe,
    _mode_recipe,
    _read_document,
    _render,
    _template_context,
)
from zhunt.launch import daemon_reachable, preflight_warnings

STALE_AFTER_DAYS = 60
POLICY_FLAGS = ("ZHUNT_ALLOW_PAID", "ZHUNT_ALLOW_TRAINING")
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"", "0", "false", "no", "off"}
_SECRET_HINTS = ("key", "token")


class Status(str, Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    message: str
    fix: str = ""

    def render(self) -> str:
        line = f"{self.status.value:<4}  {self.name}: {self.message}"
        if self.fix and self.status is not Status.OK:
            line += f"\n      fix: {self.fix}"
        return line


@dataclass(frozen=True)
class Drift:
    location: str
    expected: str
    found: str


def run_checks(
    *,
    home: Path,
    port: int,
    catalog_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    today: date | None = None,
    probe: Callable[[int], bool] = daemon_reachable,
    cwd: Path | None = None,
) -> list[Check]:
    env_file = home / ".zhunt" / "env"
    environ = os.environ if environ is None else environ
    # The daemon sees the env file with the real environment on top.
    effective = {**read_env_values(env_file), **environ}
    key = read_master_key(env_file)
    checks = [_daemon_check(port, probe), _key_check(key)]
    catalog = _load_catalog(catalog_path)
    if isinstance(catalog, Check):
        checks.append(catalog)
    else:
        checks.append(_provider_keys_check(catalog, effective))
        checks.append(_freshness_check(catalog, today or date.today()))
    checks.extend(_policy_checks(effective))
    checks.extend(_app_checks(home, port, key, cwd, environ))
    return checks


def has_failure(checks: list[Check]) -> bool:
    return any(check.status is Status.FAIL for check in checks)


def _daemon_check(port: int, probe: Callable[[int], bool]) -> Check:
    if probe(port):
        return Check(Status.OK, "daemon", f"answering on 127.0.0.1:{port}")
    return Check(
        Status.FAIL,
        "daemon",
        f"nothing answering on 127.0.0.1:{port}",
        "start it with `zhunt serve --free-tier`",
    )


def _key_check(key: str | None) -> Check:
    if key:
        return Check(Status.OK, "master key", "present in ~/.zhunt/env")
    return Check(
        Status.FAIL,
        "master key",
        "no ZHUNT_MASTER_KEY in ~/.zhunt/env",
        "run `zhunt serve --free-tier` once; it creates the key",
    )


def _load_catalog(path: Path | None) -> Catalog | Check:
    try:
        if path is not None:
            return Catalog.from_path(path)
        packaged = resources.files("zhunt").joinpath("catalog.yaml")
        with packaged.open(encoding="utf-8") as catalog_file:
            return Catalog.from_data(yaml.safe_load(catalog_file))
    except (OSError, ValueError, yaml.YAMLError) as error:
        return Check(
            Status.FAIL,
            "catalog",
            f"cannot load: {error}",
            "reinstall zhunt or pass a valid --catalog file",
        )


def _provider_keys_check(catalog: Catalog, effective: Mapping[str, str]) -> Check:
    names = sorted({provider.key_env for provider in catalog.providers.values()})
    present = [name for name in names if effective.get(name)]
    missing = [name for name in names if name not in present]
    detail = f"set: {', '.join(present) or 'none'}; missing: {', '.join(missing) or 'none'}"
    if present:
        return Check(Status.OK, "provider keys", detail)
    return Check(
        Status.FAIL,
        "provider keys",
        detail,
        "add at least one free-tier key to ~/.zhunt/env, e.g. GROQ_API_KEY=...",
    )


def _freshness_check(catalog: Catalog, today: date) -> Check:
    stale = sorted(
        (provider.platform, (today - provider.verified_on).days)
        for provider in catalog.providers.values()
        if (today - provider.verified_on).days > STALE_AFTER_DAYS
    )
    if not stale:
        return Check(Status.OK, "catalog", f"all providers verified within {STALE_AFTER_DAYS} days")
    listing = ", ".join(f"{name} ({days}d)" for name, days in stale)
    return Check(
        Status.WARN,
        "catalog",
        f"free-tier limits not re-verified for {listing}",
        "update zhunt, or re-check each provider's limits and bump verified_on",
    )


def _policy_checks(effective: Mapping[str, str]) -> list[Check]:
    checks = []
    for flag in POLICY_FLAGS:
        raw = effective.get(flag, "")
        lowered = raw.lower()
        if lowered in _TRUTHY:
            consequence = (
                "paid models may be used" if flag.endswith("PAID")
                else "providers that may train on prompts are allowed"
            )
            checks.append(
                Check(Status.WARN, flag, f"on ({consequence})", f"unset {flag} to restore the default (off)")
            )
        elif lowered in _FALSY:
            state = "unset (off)" if not raw else f"{raw} (off)"
            checks.append(Check(Status.OK, flag, state))
        else:
            checks.append(
                Check(
                    Status.WARN,
                    flag,
                    "unrecognised value, treated as off",
                    f"use 1 or unset {flag}",
                )
            )
    return checks


def _app_checks(
    home: Path,
    port: int,
    key: str | None,
    cwd: Path | None,
    environ: Mapping[str, str] | None,
) -> list[Check]:
    checks: list[Check] = []
    installed = _installed_apps(home)
    base_url = f"http://127.0.0.1:{port}"
    for app in installed:
        name = f"app {app}"
        try:
            drift = app_drift(home, app, base_url=base_url, master_key=key or "")
        except (InstallerError, OSError, ValueError, KeyError) as error:
            checks.append(
                Check(Status.WARN, name, f"cannot read configuration: {error}", f"run `zhunt launch {app}` instead")
            )
            continue
        if not drift:
            checks.append(Check(Status.OK, name, f"v1 configuration matches {base_url}"))
            continue
        lines = "; ".join(f"{d.location}: expected {d.expected}, found {d.found}" for d in drift)
        checks.append(
            Check(
                Status.WARN,
                name,
                f"v1 configuration has drifted: {lines}",
                f"run `zhunt launch {app}` (no config needed) or `zhunt install {app}` to rewrite it",
            )
        )
    for app in ("claude", "hermes"):
        for warning in preflight_warnings(app, home=home, cwd=cwd, environ=environ):
            checks.append(
                Check(Status.WARN, f"launch {app}", warning, "remove the listed entries")
            )
    return checks


def _installed_apps(home: Path) -> tuple[str, ...]:
    installs = home / ".zhunt" / "installs"
    if not installs.is_dir():
        return ()
    return tuple(
        sorted(
            path.stem
            for path in installs.glob("*.json")
            if path.stem in supported_apps()
        )
    )


def app_drift(home: Path, app: str, *, base_url: str, master_key: str) -> list[Drift]:
    """Compare an app's v1-installed config with what the current endpoint needs.

    The expected values come from the app's own install recipe, so this stays in
    step with whatever `zhunt install` would write. Secret fields are compared
    but their values are never reported.
    """

    recipe = _load_recipe(app)
    if recipe.get("format") == "manual":
        return []
    manifest = json.loads(
        (home / ".zhunt" / "installs" / f"{app}.json").read_text(encoding="utf-8")
    )
    target = Path(str(manifest["target"]))
    if not target.is_file():
        return [Drift("config file", str(target), "missing")]
    modes = recipe.get("modes", {})
    # Passthrough operations are a subset of api ones, and claude has only api.
    passthrough = modes.get("passthrough", {})
    mode = "passthrough" if passthrough.get("supported", True) is True else "api"
    operations = _render(
        _mode_recipe(recipe, mode).get("operations", []),
        _template_context(base_url, master_key=master_key or "(no key)"),
    )
    document = _read_document(
        target,
        recipe_format=str(recipe["format"]),
        root=str(recipe.get("root", "mapping")),
    )
    drift: list[Drift] = []
    for operation in operations:
        action = operation.get("action")
        path = list(operation.get("path", []))
        if action == "set":
            _compare(document, [*path[:-1]], {path[-1]: operation.get("value")}, drift)
        elif action == "merge":
            _compare(document, path, operation.get("value", {}), drift)
        elif action == "upsert_list":
            _compare_list_entry(
                document, path, operation.get("match", {}), operation.get("value", {}), drift
            )
    return drift


def _compare(node: Any, path: list[str], expected: Mapping[str, Any], drift: list[Drift]) -> None:
    where = ".".join(path)
    current: Any = node
    for part in path:
        current = current.get(part) if isinstance(current, Mapping) else None
    _compare_values(current, expected, where, drift)


def _compare_list_entry(
    node: Any,
    path: list[str],
    match: Mapping[str, Any],
    expected: Mapping[str, Any],
    drift: list[Drift],
) -> None:
    current: Any = node
    for part in path:
        current = current.get(part) if isinstance(current, Mapping) else None
    where = ".".join(path)
    entries = current if isinstance(current, list) else []
    label = ".".join([where, f"[{', '.join(f'{k}={v}' for k, v in match.items())}]"]).strip(".")
    for entry in entries:
        if isinstance(entry, Mapping) and all(entry.get(k) == v for k, v in match.items()):
            _compare_values(entry, expected, label, drift)
            return
    drift.append(Drift(label, "an entry", "missing"))


def _compare_values(found: Any, expected: Any, where: str, drift: list[Drift]) -> None:
    if isinstance(expected, Mapping):
        for name, value in expected.items():
            child = found.get(name) if isinstance(found, Mapping) else None
            _compare_values(child, value, f"{where}.{name}" if where else str(name), drift)
        return
    if isinstance(expected, list):
        for index, value in enumerate(expected):
            child = found[index] if isinstance(found, list) and index < len(found) else None
            _compare_values(child, value, f"{where}[{index}]", drift)
        return
    if found == expected:
        return
    leaf = where.rsplit(".", 1)[-1].lower()
    if any(hint in leaf for hint in _SECRET_HINTS):
        drift.append(
            Drift(where, "the current master key", "missing" if found is None else "a different value")
        )
    else:
        drift.append(Drift(where, str(expected), "missing" if found is None else str(found)))
