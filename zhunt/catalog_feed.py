"""Signed free-model catalog feed: the catalog between releases.

zhunt never fetches it on its own. A request leaves the machine only when the user
runs ``zhunt catalog update`` or turns on catalog updates (a Pro switch in the
Connect window, saved as ZHUNT_CATALOG_UPDATES=1 in ~/.zhunt/env). Each check is
one plain GET of a static file with no cookies, key, licence or other identifier:

  latest.json    the current catalog (Pro: checked every 12 hours when switched on)
  monthly.json   a snapshot taken once a month (Free: ``zhunt catalog update``)

A feed file is JSON ``{"format": "zhunt-catalog", "kid", "payload", "sig"}``: payload
is base64url JSON ``{"v": 1, "kid", "channel", "version", "issued_at", "expires_at",
"catalog": {...}}`` and sig an Ed25519 signature over ``ZHUNTCAT1.<payload>``, made
with a catalog key that is separate from the licence key (scripts/catalog_tool.py).
A feed is used only when its signature, expiry and schema check out and its version
is newer than the catalog in use; the verified file is cached in ~/.zhunt/catalog.json
and checked again on every load. Anything else keeps the catalog shipped with zhunt.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zhunt import licence, llm
from zhunt.auth import write_private_text
from zhunt.catalog import Catalog, CatalogError

FORMAT = "zhunt-catalog"
SIGNING_PREFIX = b"ZHUNTCAT1."
BASE_URL = "https://zhunt.kindredwildcat.com/catalog"
CHANNEL_FILES = {"latest": "latest.json", "monthly": "monthly.json"}
UPDATES_ENV = "ZHUNT_CATALOG_UPDATES"
# For a self-hosted feed or a local test: another base URL, and a key to trust ("kid=base64url").
URL_ENV = "ZHUNT_CATALOG_URL"
PUBKEY_ENV = "ZHUNT_CATALOG_PUBKEY"
CACHE_ENV = "ZHUNT_CATALOG_CACHE"
MAX_BYTES = 2_000_000
FETCH_TIMEOUT = 20.0
REFRESH_SECONDS = 12 * 3600
RETRY_SECONDS = 3600
WATCH_SECONDS = 30.0
BOOT_DELAY = 10.0
# Routes zhunt.llm can reach; a provider on any other route could never be called.
ROUTES = frozenset(llm._ROUTES)

# Public halves of the catalog signing keys, by key id. Separate from the licence keys;
# the private key never enters the repo. Paste the line `catalog_tool.py keygen` prints.
PUBLIC_KEYS: dict[str, str] = {
    'cat-2026-10': 'FEJbBUT7Q5OvEL-q-c01km_9WxcqDlj-jy7Lk7uXbho',
}

_TRUTHY = {"1", "true", "yes", "on"}


class FeedError(ValueError):
    """A feed is unusable; the message says why and is safe to show."""


@dataclass(frozen=True)
class ActiveCatalog:
    """The catalog the engine routes with, and where it came from."""

    catalog: Catalog
    version: int
    source: str  # "bundled", "feed" or "file" (--catalog)
    channel: str | None = None
    issued_at: float | None = None
    expires_at: float | None = None
    note: str | None = None  # why a cached feed is not in use, if one exists

    def to_api(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "channel": self.channel,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "note": self.note,
            "providers": len(self.catalog.providers),
            "models": len(self.catalog.models),
        }


@dataclass(frozen=True)
class UpdateResult:
    status: str  # "applied", "current", "older" or "error"
    message: str
    active: ActiveCatalog


def _b64decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise FeedError("the catalog is damaged (bad encoding)") from error


def trusted_keys(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    keys = dict(PUBLIC_KEYS)
    kid, sep, public = env.get(PUBKEY_ENV, "").partition("=")
    if sep and kid.strip() and public.strip():
        keys[kid.strip()] = public.strip()
    return keys


def verify_feed(
    raw: bytes, *, now: float | None = None, public_keys: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], Catalog]:
    """Return the verified payload and its catalog, or raise FeedError."""

    now = time.time() if now is None else now
    keys = trusted_keys() if public_keys is None else public_keys
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise FeedError("the catalog is not JSON") from error
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT:
        raise FeedError("this is not a zhunt catalog feed")
    payload_b64, sig = envelope.get("payload"), envelope.get("sig")
    if not isinstance(payload_b64, str) or not isinstance(sig, str):
        raise FeedError("the catalog is not signed")
    kid = str(envelope.get("kid"))
    public = keys.get(kid)
    if public is None:
        if not keys:
            raise FeedError("this zhunt has no catalog signing key yet, so it cannot check a feed")
        raise FeedError(f"the catalog was signed by an unknown key ({kid}); update zhunt")
    try:
        Ed25519PublicKey.from_public_bytes(_b64decode(public)).verify(
            _b64decode(sig), SIGNING_PREFIX + payload_b64.encode()
        )
    except (InvalidSignature, ValueError) as error:
        raise FeedError("the catalog's signature does not match (it was altered in transit or forged)") from error
    try:
        payload = json.loads(_b64decode(payload_b64))
    except ValueError as error:
        raise FeedError("the catalog is damaged (bad payload)") from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise FeedError("this catalog is for a newer zhunt; update zhunt")
    if payload.get("kid") != kid:
        raise FeedError("the catalog's key id does not match its signature")
    if payload.get("channel") not in CHANNEL_FILES:
        raise FeedError("the catalog has an unknown channel")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise FeedError("the catalog has no valid version")
    issued, expires = payload.get("issued_at"), payload.get("expires_at")
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in (issued, expires)):
        raise FeedError("the catalog has no issue or expiry time")
    if now > expires:
        raise FeedError(f"the catalog expired on {_day(expires)}")
    if issued > now + 86400:
        raise FeedError("the catalog is dated in the future (check this computer's clock)")
    try:
        catalog = Catalog.from_data(payload.get("catalog"))
    except CatalogError as error:
        raise FeedError(f"the catalog failed validation: {error}") from error
    unknown = sorted({p.litellm_provider for p in catalog.providers.values()} - ROUTES)
    if unknown:
        raise FeedError(f"the catalog needs a newer zhunt (unknown routes: {', '.join(unknown)})")
    return payload, catalog


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def bundled() -> ActiveCatalog:
    """The catalog shipped inside the package; its ``version`` is the floor for feeds."""

    packaged = resources.files("zhunt").joinpath("catalog.yaml")
    with packaged.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    version = data.get("version", 0) if isinstance(data, dict) else 0
    return ActiveCatalog(Catalog.from_data(data), int(version), "bundled")


def cache_path(home: Path | None = None) -> Path:
    override = os.environ.get(CACHE_ENV) if home is None else None
    return Path(override) if override else (home or Path.home()) / ".zhunt" / "catalog.json"


def _from_feed(payload: Mapping[str, Any], catalog: Catalog) -> ActiveCatalog:
    return ActiveCatalog(
        catalog,
        payload["version"],
        "feed",
        channel=payload["channel"],
        issued_at=float(payload["issued_at"]),
        expires_at=float(payload["expires_at"]),
    )


def load_active(
    home: Path | None = None, *, now: float | None = None, public_keys: Mapping[str, str] | None = None
) -> ActiveCatalog:
    """The cached feed if it still verifies and is newer than the bundled catalog,
    otherwise the bundled catalog (with a note saying why). Never touches the network."""

    base = bundled()
    path = cache_path(home)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return base
    except OSError as error:
        return replace(base, note=f"cannot read {path}: {error.strerror}")
    try:
        payload, catalog = verify_feed(raw, now=now, public_keys=public_keys)
    except FeedError as error:
        return replace(base, note=f"cached catalog not used: {error}")
    if payload["version"] <= base.version:
        return base  # this zhunt shipped with the same or a newer catalog
    return _from_feed(payload, catalog)


def feed_url(channel: str, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    base = (env.get(URL_ENV) or BASE_URL).rstrip("/")
    return f"{base}/{CHANNEL_FILES[channel]}"


def fetch(url: str, *, timeout: float = FETCH_TIMEOUT) -> bytes:
    """One GET, with no cookies, key or identifier (the User-Agent is just "zhunt")."""

    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise FeedError("the catalog URL must use https://")
    request = urllib.request.Request(url, headers={"User-Agent": "zhunt", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked)
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise FeedError(f"the catalog server answered HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise FeedError(f"could not reach the catalog server ({reason})") from error
    if len(body) > MAX_BYTES:
        raise FeedError("the catalog is too large")
    return body


def channel_for(plan: licence.Plan) -> str:
    return "latest" if plan.has("catalog_live") else "monthly"


def update(
    channel: str,
    *,
    current: ActiveCatalog | None = None,
    home: Path | None = None,
    now: float | None = None,
    public_keys: Mapping[str, str] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    env: Mapping[str, str] | None = None,
) -> UpdateResult:
    """Fetch, verify and cache the ``channel`` feed when it is newer than ``current``."""

    current = current or load_active(home, now=now, public_keys=public_keys)
    try:
        raw = (fetcher or fetch)(feed_url(channel, env))
        payload, catalog = verify_feed(raw, now=now, public_keys=public_keys)
    except FeedError as error:
        return UpdateResult("error", f"kept catalog version {current.version}: {error}", current)
    version = payload["version"]
    if version < current.version:
        return UpdateResult(
            "older",
            f"kept catalog version {current.version}: the {channel} feed is older ({version}); zhunt never downgrades",
            current,
        )
    if version == current.version:
        return UpdateResult("current", f"catalog version {version} is up to date", current)
    write_private_text(cache_path(home), raw.decode("utf-8"))
    active = _from_feed(payload, catalog)
    return UpdateResult(
        "applied",
        f"catalog updated to version {version} ({len(catalog.models)} models from {len(catalog.providers)} providers)",
        active,
    )


def updates_switched_on(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get(UPDATES_ENV, "").strip().lower() in _TRUTHY


class CatalogFeed:
    """The daemon's side: holds the active catalog, applies updates without a restart.

    ``apply`` is called with each newly active catalog. Nothing here fetches unless
    the switch is on and the plan includes live updates, or ``update_now`` is called.
    """

    def __init__(
        self,
        active: ActiveCatalog,
        apply: Callable[[ActiveCatalog], None],
        *,
        home: Path | None = None,
        plan: Callable[[], licence.Plan] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        public_keys: Mapping[str, str] | None = None,
    ) -> None:
        self.active = active
        self._apply = apply
        self._home = home
        self._plan = plan or licence.current_plan
        self._fetcher = fetcher
        self._public_keys = public_keys
        self.last_checked: float | None = None
        self.last_result: UpdateResult | None = None
        self._stamp = self._cache_stamp()

    def enabled(self) -> bool:
        return updates_switched_on() and self._plan().has("catalog_live")

    def status(self) -> dict[str, Any]:
        plan = self._plan()
        return {
            "active": self.active.to_api(),
            "updates": {
                "switched_on": updates_switched_on(),
                "available": plan.has("catalog_live"),
                "enabled": updates_switched_on() and plan.has("catalog_live"),
                "channel": channel_for(plan),
                "url": feed_url(channel_for(plan)),
                "every_hours": REFRESH_SECONDS // 3600,
            },
            "last_checked": self.last_checked,
            "last_status": self.last_result.status if self.last_result else None,
            "last_message": self.last_result.message if self.last_result else None,
        }

    def update_now(self) -> UpdateResult:
        result = update(
            channel_for(self._plan()),
            current=self.active,
            home=self._home,
            public_keys=self._public_keys,
            fetcher=self._fetcher,
        )
        self.last_checked = time.time()
        self.last_result = result
        if result.status == "applied":
            self._switch(result.active)
        self._stamp = self._cache_stamp()
        return result

    def reload_if_changed(self) -> bool:
        """Pick up a cache written by `zhunt catalog update` in another process."""

        stamp = self._cache_stamp()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        active = load_active(self._home, public_keys=self._public_keys)
        if active.version > self.active.version:
            self._switch(active)
            return True
        return False

    def _switch(self, active: ActiveCatalog) -> None:
        self._apply(active)
        self.active = active

    def _cache_stamp(self) -> tuple[int, int] | None:
        try:
            stat = cache_path(self._home).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    async def run(self) -> None:
        """Watch the local cache file; fetch only while updates are enabled."""

        next_fetch = time.monotonic() + BOOT_DELAY
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            self.reload_if_changed()
            if self.enabled() and time.monotonic() >= next_fetch:
                result = await asyncio.to_thread(self.update_now)
                print(f"zhunt catalog: {result.message}")
                wait = RETRY_SECONDS if result.status == "error" else REFRESH_SECONDS
                next_fetch = time.monotonic() + wait
