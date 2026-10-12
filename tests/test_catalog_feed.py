"""Signed catalog feed: verification, caching, no downgrades, hot reload, privacy."""

import asyncio
import importlib.util
import time
import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.catalog_fixtures import catalog_data, model, provider
from zhunt import catalog_feed as feed
from zhunt.brain import Tier
from zhunt.catalog import Catalog
from zhunt.cli import app as cli_app
from zhunt.licence import FEATURES, FREE, Plan
from zhunt.quota import FailureKind
from zhunt.server import create_proxy_app
from zhunt.workspace_api import WorkspaceDirs

_spec = importlib.util.spec_from_file_location(
    "catalog_tool", Path(__file__).resolve().parents[1] / "scripts" / "catalog_tool.py"
)
catalog_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catalog_tool)

NOW = 1_800_000_000
PRIVATE = Ed25519PrivateKey.generate()
KID = "test-kid"
KEYS = {KID: catalog_tool.b64url(catalog_tool.public_raw(PRIVATE))}
PRO = Plan(name="pro", features=FEATURES["pro"], expires_at=2e9)


def routable(data: dict) -> dict:
    for item in data["providers"].values():
        item["litellm_provider"] = "openai"
    return data


def base_data() -> dict:
    return routable(catalog_data())


def newer_data() -> dict:
    data = base_data()
    data["providers"]["alpha"]["models"].append(model("brand-new", ["chat"], intelligence=99, speed=99))
    return data


def signed(data: dict | None = None, *, version: int = 200, channel: str = "latest", now: int = NOW,
           days: int = 30, private: Ed25519PrivateKey = PRIVATE, kid: str = KID) -> bytes:
    return catalog_tool.sign_feed(private, kid, channel, version, data or newer_data(), now=now, days=days)


def bundled_v100() -> feed.ActiveCatalog:
    return feed.ActiveCatalog(Catalog.from_data(base_data()), 100, "bundled")


@pytest.fixture
def floor(monkeypatch):
    """A bundled catalog at version 100 and the test signing key as the only trusted key."""

    monkeypatch.setattr(feed, "bundled", bundled_v100)
    monkeypatch.setattr(feed, "PUBLIC_KEYS", dict(KEYS))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("zhunt made a network request")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.setattr(feed, "fetch", refuse)


# -- verification -----------------------------------------------------------


def test_a_signed_feed_verifies() -> None:
    payload, catalog = feed.verify_feed(signed(), now=NOW, public_keys=KEYS)
    assert payload["version"] == 200 and payload["channel"] == "latest"
    assert "alpha/brand-new" in {m.id for m in catalog.models}


def tamper(raw: bytes) -> bytes:
    envelope = json.loads(raw)
    payload = json.loads(feed._b64decode(envelope["payload"]))
    payload["catalog"]["providers"]["alpha"]["base_url"] = "https://evil.invalid/v1"
    envelope["payload"] = catalog_tool.b64url(json.dumps(payload).encode())
    return json.dumps(envelope).encode()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (lambda: tamper(signed()), "signature does not match"),
        (lambda: signed(private=Ed25519PrivateKey.generate()), "signature does not match"),
        (lambda: signed(kid="someone-else"), "unknown key"),
        (lambda: signed(now=NOW - 31 * 86400), "expired"),
        (lambda: signed(now=NOW + 3 * 86400), "dated in the future"),
        (lambda: b"<html>not json</html>", "not JSON"),
        (lambda: json.dumps({"format": "zhunt-catalog", "kid": KID}).encode(), "not signed"),
    ],
)
def test_bad_feeds_are_rejected(raw, message) -> None:
    with pytest.raises(feed.FeedError, match=message):
        feed.verify_feed(raw(), now=NOW, public_keys=KEYS)


def test_a_feed_failing_the_catalog_schema_is_rejected() -> None:
    data = newer_data()
    del data["providers"]["alpha"]["models"][0]["may_train_on_prompts"]
    with pytest.raises(feed.FeedError, match="failed validation"):
        feed.verify_feed(signed(data), now=NOW, public_keys=KEYS)


def test_a_feed_with_a_route_this_zhunt_cannot_call_is_rejected() -> None:
    data = newer_data()
    data["providers"]["alpha"]["litellm_provider"] = "some-new-sdk"
    with pytest.raises(feed.FeedError, match="newer zhunt"):
        feed.verify_feed(signed(data), now=NOW, public_keys=KEYS)


def test_without_any_trusted_key_nothing_verifies() -> None:
    with pytest.raises(feed.FeedError, match="no catalog signing key"):
        feed.verify_feed(signed(), now=NOW, public_keys={})


def test_the_licence_key_cannot_sign_catalogs() -> None:
    from zhunt.licence import PUBLIC_KEYS as licence_keys

    assert not set(feed.PUBLIC_KEYS.values()) & set(licence_keys.values())


# -- cache and fallback -------------------------------------------------------


def test_without_a_cache_the_bundled_catalog_is_used(floor) -> None:
    active = feed.load_active(now=NOW)
    assert (active.source, active.version, active.note) == ("bundled", 100, None)


def test_a_verified_newer_cache_is_used(floor) -> None:
    feed.cache_path().write_bytes(signed())
    active = feed.load_active(now=NOW)
    assert (active.source, active.version, active.channel) == ("feed", 200, "latest")


@pytest.mark.parametrize(
    ("raw", "note"),
    [
        (lambda: tamper(signed()), "signature does not match"),
        (lambda: signed(now=NOW - 40 * 86400), "expired"),
        (lambda: b"garbage", "not JSON"),
    ],
)
def test_a_bad_cache_falls_back_to_bundled_and_says_why(floor, raw, note) -> None:
    feed.cache_path().write_bytes(raw())
    active = feed.load_active(now=NOW)
    assert active.source == "bundled" and active.version == 100
    assert note in active.note


def test_a_cache_older_than_the_bundled_catalog_is_ignored(floor) -> None:
    feed.cache_path().write_bytes(signed(version=50))
    assert feed.load_active(now=NOW).source == "bundled"


def test_the_real_bundled_catalog_has_a_version_floor() -> None:
    active = feed.bundled()
    assert active.version >= 202610100000 and active.source == "bundled"


# -- update -----------------------------------------------------------------------


def test_update_applies_and_caches_a_newer_feed_owner_only(floor) -> None:
    urls = []
    result = feed.update("monthly", now=NOW, fetcher=lambda url: urls.append(url) or signed(channel="monthly"))
    assert result.status == "applied" and result.active.version == 200
    assert urls == ["https://zhunt.kindredwildcat.com/catalog/monthly.json"]
    path = feed.cache_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert feed.load_active(now=NOW).version == 200


def test_update_never_downgrades(floor) -> None:
    feed.cache_path().write_bytes(signed(version=300))
    before = feed.cache_path().read_bytes()
    result = feed.update("monthly", now=NOW, fetcher=lambda url: signed(version=200, channel="monthly"))
    assert result.status == "older" and "never downgrades" in result.message
    assert result.active.version == 300
    assert feed.cache_path().read_bytes() == before


def test_update_with_the_same_version_is_up_to_date(floor) -> None:
    feed.cache_path().write_bytes(signed())
    result = feed.update("latest", now=NOW, fetcher=lambda url: signed())
    assert result.status == "current"


@pytest.mark.parametrize(
    "fetcher",
    [
        lambda url: tamper(signed()),
        lambda url: signed(now=NOW - 40 * 86400),
        lambda url: (_ for _ in ()).throw(feed.FeedError("could not reach the catalog server (offline)")),
    ],
)
def test_a_failed_update_keeps_the_catalog_and_writes_nothing(floor, fetcher) -> None:
    result = feed.update("latest", now=NOW, fetcher=fetcher)
    assert result.status == "error" and result.message.startswith("kept catalog version 100")
    assert result.active.source == "bundled"
    assert not feed.cache_path().exists()


def test_feed_url_can_point_at_a_self_hosted_feed() -> None:
    assert feed.feed_url("latest", {feed.URL_ENV: "http://127.0.0.1:9/c/"}) == "http://127.0.0.1:9/c/latest.json"


def test_fetch_refuses_plain_http_off_this_machine() -> None:
    with pytest.raises(feed.FeedError, match="https"):
        feed.fetch("http://zhunt.kindredwildcat.com/catalog/latest.json")


def test_fetch_sends_no_identifiers(monkeypatch) -> None:
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return b"{}"

    def urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    feed.fetch("https://zhunt.kindredwildcat.com/catalog/latest.json")
    assert seen["method"] == "GET"
    assert seen["url"] == "https://zhunt.kindredwildcat.com/catalog/latest.json"
    assert seen["headers"] == {"User-agent": "zhunt", "Accept": "application/json"}


def test_trusted_keys_accepts_a_self_hosted_key() -> None:
    assert feed.trusted_keys({feed.PUBKEY_ENV: "mine=abc"})["mine"] == "abc"


# -- the daemon: switch, plan, hot reload ----------------------------------------------


def test_the_daemon_never_fetches_while_updates_are_off(floor, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(feed, "WATCH_SECONDS", 0)
    monkeypatch.setattr(feed, "BOOT_DELAY", 0)

    async def tick(plan, switched) -> None:
        monkeypatch.setenv(feed.UPDATES_ENV, "1" if switched else "0")
        watcher = feed.CatalogFeed(bundled_v100(), lambda a: None, plan=lambda: plan,
                                   fetcher=lambda url: calls.append(url) or signed())
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(tick(PRO, False))  # Pro, switch off
    asyncio.run(tick(FREE, True))  # switch on, but not Pro
    assert calls == []
    asyncio.run(tick(PRO, True))
    assert calls == ["https://zhunt.kindredwildcat.com/catalog/latest.json"]


def test_the_feed_watcher_picks_up_a_cache_written_by_the_cli(floor) -> None:
    applied = []
    watcher = feed.CatalogFeed(bundled_v100(), applied.append)
    assert watcher.reload_if_changed() is False
    feed.cache_path().write_bytes(signed(now=int(time.time())))
    assert watcher.reload_if_changed() is True
    assert [a.version for a in applied] == [200]
    assert watcher.reload_if_changed() is False


@pytest.fixture
def daemon(floor, tmp_path, monkeypatch):
    """The real daemon on the (patched) bundled catalog, with a feed watcher."""

    root = tmp_path
    dirs = WorkspaceDirs(layouts=root / "l", themes=root / "t", static=root / "s", env_file=root / "env",
                         bots=root / "b", wallpaper=root / "w")
    for name in ("FAKE_API_KEY",):
        monkeypatch.setenv(name, "sk-fake-key-value")
    plan = {"now": FREE}
    monkeypatch.setattr("zhunt.licence.current_plan", lambda *a, **k: plan["now"])
    app = create_proxy_app(env_path=root / "zhunt.env", workspace_dirs=dirs)
    with TestClient(app) as client:
        client.plan = plan
        client.hdr = {"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}"}
        yield client, app.hook


def test_starting_the_daemon_fetches_nothing(no_network, daemon) -> None:
    client, hook = daemon
    assert client.get("/zhunt/api/status", headers=client.hdr).json()["catalog"]["version"] == 100
    assert client.get("/zhunt/api/catalog", headers=client.hdr).json()["updates"]["enabled"] is False


def test_hot_reload_swaps_the_catalog_and_keeps_quota_state(daemon) -> None:
    client, hook = daemon
    slot = ("alpha/coder", "default")
    hook.quota.record_failure(slot, FailureKind.SERVER)
    cooldown = hook.quota.cooldown_remaining(slot)
    assert cooldown > 0
    hook.reliability.record("alpha/coder", success=False)
    reliability = hook.reliability.mean("alpha/coder")

    hook.catalog_feed._fetcher = lambda url: signed(now=int(time.time()), channel="monthly")
    result = hook.catalog_feed.update_now()

    assert result.status == "applied"
    assert "alpha/brand-new" in {m.id for m in hook.catalog.models}
    assert hook.selector.ranked(Tier.CHAT, tokens=10)[0].model.id == "alpha/brand-new"
    assert hook.quota.cooldown_remaining(slot) > 0
    assert hook.reliability.mean("alpha/coder") == pytest.approx(reliability, abs=1e-6)
    status = client.get("/zhunt/api/status", headers=client.hdr).json()
    assert status["catalog"]["version"] == 200
    assert "alpha/brand-new" in {m["id"] for p in status["providers"] for m in p["models"]}


def test_the_connect_switch_is_pro_only_and_fetches_when_turned_on(daemon, tmp_path) -> None:
    client, hook = daemon
    urls = []
    hook.catalog_feed._fetcher = lambda url: urls.append(url) or signed(now=int(time.time()))

    refused = client.put("/zhunt/api/catalog/updates", headers=client.hdr, json={"enabled": True})
    assert refused.status_code == 403 and urls == []

    client.plan["now"] = PRO
    hook._plan_cache = None
    on = client.put("/zhunt/api/catalog/updates", headers=client.hdr, json={"enabled": True})
    assert on.status_code == 200, on.text
    assert on.json()["status"] == "applied" and on.json()["updates"]["enabled"] is True
    assert urls == ["https://zhunt.kindredwildcat.com/catalog/latest.json"]
    assert 'ZHUNT_CATALOG_UPDATES="1"' in (tmp_path / "env").read_text()

    off = client.put("/zhunt/api/catalog/updates", headers=client.hdr, json={"enabled": False})
    assert off.json()["updates"]["switched_on"] is False
    assert len(urls) == 1


def test_update_now_from_the_workspace_uses_the_monthly_snapshot_on_free(daemon) -> None:
    client, hook = daemon
    urls = []
    hook.catalog_feed._fetcher = lambda url: urls.append(url) or b"nope"
    body = client.post("/zhunt/api/catalog/update", headers=client.hdr).json()
    assert body["status"] == "error" and "not JSON" in body["message"]
    assert body["active"]["version"] == 100
    assert urls == ["https://zhunt.kindredwildcat.com/catalog/monthly.json"]


def test_catalog_routes_need_the_master_key(daemon) -> None:
    client, _ = daemon
    for method, path in (("GET", "/zhunt/api/catalog"), ("POST", "/zhunt/api/catalog/update"),
                         ("PUT", "/zhunt/api/catalog/updates")):
        assert client.request(method, path, json={}).status_code == 401


# -- CLI ---------------------------------------------------------------------------


def test_cli_status_fetches_nothing(floor, no_network) -> None:
    result = CliRunner().invoke(cli_app, ["catalog", "status"])
    assert result.exit_code == 0, result.output
    assert "version 100, shipped with this zhunt" in result.output
    assert "monthly.json" in result.output


def test_cli_update_applies_then_reports_up_to_date(floor, monkeypatch) -> None:
    monkeypatch.setattr(feed, "fetch", lambda url: signed(now=int(time.time()), channel="monthly"))
    first = CliRunner().invoke(cli_app, ["catalog", "update"])
    assert first.exit_code == 0, first.output
    assert "Catalog updated to version 200" in first.output
    second = CliRunner().invoke(cli_app, ["catalog", "update"])
    assert "up to date" in second.output
    assert "signed monthly feed" in CliRunner().invoke(cli_app, ["catalog", "status"]).output


def test_cli_update_failure_exits_non_zero_and_keeps_bundled(floor, monkeypatch) -> None:
    monkeypatch.setattr(feed, "fetch", lambda url: tamper(signed()))
    result = CliRunner().invoke(cli_app, ["catalog", "update"])
    assert result.exit_code == 1
    assert "signature does not match" in result.output
    assert not feed.cache_path().exists()


def _check(transport, keys, tmp_path, verified="2026-10-10"):
    path = tmp_path / "catalog.yaml"
    path.write_text(
        "version: 1\nproviders:\n"
        f"  groq: {{name: Groq, base_url: 'https://g.test/v1', key_env: GROQ_API_KEY, verified_on: {verified}, models: [{{id: a}}, {{id: b}}]}}\n"
        f"  kilo: {{name: Kilo, base_url: 'https://k.test', key_env: KILO_API_KEY, keyless: true, verified_on: {verified}, models: [{{id: x/free}}]}}\n"
        f"  cerebras: {{name: Cerebras, base_url: 'https://c.test/v1', key_env: CEREBRAS_API_KEY, verified_on: {verified}, models: [{{id: c}}]}}\n"
        f"  openai: {{name: OpenAI, base_url: 'https://o.test/v1', key_env: OPENAI_API_KEY, verified_on: {verified}, models: [{{id: p, in: 0.1, out: 0.2}}]}}\n"
    )
    import httpx
    from datetime import date

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        return catalog_tool.check(path, today=date(2026, 10, 12), keys=keys, client=client)


def test_check_reports_new_and_gone_models_and_skips_paid_and_keyless_gaps(tmp_path):
    seen = []

    def transport(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "g.test":
            return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "new-one"}]})
        return httpx.Response(200, json={"data": [{"id": "x/free"}, {"id": "y:free"}, {"id": "paid/model"}]})

    import httpx

    report, attention = _check(transport, {"GROQ_API_KEY": "k1"}, tmp_path)
    assert attention
    assert "No longer listed" in report and "`b`" in report and "`new-one`" in report
    assert "`y:free`" in report and "paid/model" not in report  # gateways: only free ids
    assert "no `CEREBRAS_API_KEY`" in report and "OpenAI" not in report
    assert ("g.test", "Bearer k1") in seen and ("k.test", None) in seen


def test_check_is_quiet_when_lists_match_and_flags_stale_terms(tmp_path):
    import httpx

    def transport(request):
        ids = ["a", "b"] if request.url.host == "g.test" else ["x/free"]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})

    keys = {"GROQ_API_KEY": "k", "CEREBRAS_API_KEY": "k"}
    report, attention = _check(lambda r: transport(r) if r.url.host != "c.test" else httpx.Response(200, json={"data": [{"id": "c"}]}), keys, tmp_path)
    assert not attention and "Nothing needs a look." in report
    report, attention = _check(transport, {}, tmp_path, verified="2026-09-01")
    assert attention and "not re-read for 41 days" in report
