import httpx
from types import SimpleNamespace
"""Workspace API (docs/WORKSPACE_API.md) driven through the real daemon app."""

import asyncio
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.catalog_fixtures import catalog_data, model, provider
from tests.test_engine import CHAT, ok, provider_error
from zhunt import llm
from zhunt.server import create_proxy_app
from zhunt.licence import FEATURES, FREE, Plan
from zhunt.workspace_api import MAX_LAYOUT_BYTES, WorkspaceDirs

KEY = "sk-super-secret-key-value"
ZETA_KEY = "zeta-secret-key-value"
SECRETS = (KEY, ZETA_KEY)

ENV_NAMES = (
    "ZHUNT_ALLOW_PAID",
    "ZHUNT_ALLOW_TRAINING",
    "FAKE_API_KEY",
    "ZETA_API_KEY",
    "EPS_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "ZHUNT_EMAIL_ADDRESS",
    "ZHUNT_EMAIL_PASSWORD",
    "ZHUNT_EMAIL_IMAP",
    "ZHUNT_EMAIL_SMTP",
)


def catalog() -> dict:
    """The engine tests' fake catalog plus an unlimited provider and a keyless one."""

    data = catalog_data()
    data["providers"]["zeta"] = provider(
        [
            model("z1", ["chat"], intelligence=10, speed=10),
            model("z2", ["chat"], intelligence=10, speed=10, limits={"rpd": 2}),
        ],
        name="Zeta",
        key_env="ZETA_API_KEY",
    )
    data["providers"]["epsilon"] = provider(
        [model("e1", ["chat"])], name="Epsilon", key_env="EPS_API_KEY"
    )
    return data


def solo_catalog() -> dict:
    return {"providers": {"zeta": provider([model("only", ["chat"])], key_env="ZETA_API_KEY")}}


def headers(key: str | None = None, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key or os.environ['ZHUNT_MASTER_KEY']}", **extra}


@contextmanager
def workspace(call: AsyncMock, *, env: dict[str, str], data: dict | None = None, static=True, plan=None):
    """Free-tier daemon with the workspace mounted on temp dirs; provider call patched."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "catalog.yaml").write_text(yaml.safe_dump(data or catalog()))
        static_dir = root / "static"
        if static:
            (static_dir / "css" / "themes").mkdir(parents=True)
            (static_dir / "index.html").write_text("<h1>workspace</h1>")
            (static_dir / "css" / "themes" / "dark.css").write_text(":root{--bg:#000}")
            (static_dir / "css" / "themes" / "light.css").write_text(":root{--bg:#fff}")
        dirs = WorkspaceDirs(layouts=root / "home" / "layouts", themes=root / "home" / "themes", static=static_dir, env_file=root / "home" / "env", bots=root / "home" / "bots", wallpaper=root / "home" / "wallpaper")
        with patch.dict(os.environ, {**dict.fromkeys(ENV_NAMES, ""), **env}), patch(
            "zhunt.llm.acompletion", call
        ), patch("zhunt.workspace_api.current_plan", return_value=plan or FREE), patch(
            "zhunt.licence.current_plan", return_value=plan or FREE
        ):  # never the tester's real licence
            app = create_proxy_app(
                catalog_path=root / "catalog.yaml",
                env_path=root / "zhunt.env",
                workspace_dirs=dirs,
            )
            with TestClient(app) as client:
                client.dirs = dirs
                yield client, app.hook


def chat(client: TestClient, session: str | None = None, body: dict = CHAT):
    extra = {"x-session-id": session} if session else {}
    return client.post("/v1/chat/completions", headers=headers(**extra), json=body)


def compare(client: TestClient, models: list[str], content: str = "hi", **extra):
    return client.post(
        "/zhunt/api/compare",
        headers=headers(),
        json={"messages": [{"role": "user", "content": content}], "models": models, **extra},
    )


def results(response) -> dict[str, dict]:
    assert response.status_code == 200, response.text
    return {r["model"]: r for r in response.json()["results"]}


ENV = {"FAKE_API_KEY": KEY}

ROUTES = [
    ("GET", "/zhunt/api/status"),
    ("GET", "/zhunt/api/decisions"),
    ("POST", "/zhunt/api/compare"),
    ("GET", "/zhunt/api/layouts"),
    ("GET", "/zhunt/api/layouts/default"),
    ("PUT", "/zhunt/api/layouts/default"),
    ("DELETE", "/zhunt/api/layouts/default"),
    ("GET", "/zhunt/api/themes"),
    ("GET", "/zhunt/api/themes/dark"),
]


# -- auth + mounting -------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_requires_the_master_key(method: str, path: str) -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        missing = client.request(method, path, json={})
        wrong = client.request(method, path, headers=headers("not-the-key"), json={})
        right = client.request(method, path, headers=headers(), json={})

    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == {"detail": "invalid Zhunt master key"}
    assert right.status_code != 401


def test_a_rejected_request_does_not_touch_disk_or_providers() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={**ENV, "ZETA_API_KEY": ZETA_KEY}) as (client, _):
        client.put("/zhunt/api/layouts/sneaky", json={"a": 1})
        client.delete("/zhunt/api/layouts/sneaky")
        client.post("/zhunt/api/compare", json={"messages": [{"role": "user", "content": "x"}], "models": ["zeta/z1"]})

        assert not client.dirs.layouts.exists()
    call.assert_not_awaited()


def test_static_files_are_served_without_a_key() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        page = client.get("/workspace/")
        missing = client.get("/workspace/nope.js")

    assert page.status_code == 200 and "workspace" in page.text
    assert missing.status_code == 404


def test_without_a_packaged_workspace_directory_only_the_static_mount_is_missing() -> None:
    with workspace(AsyncMock(), env=ENV, static=False) as (client, _):
        assert client.get("/workspace/").status_code == 404
        assert client.get("/zhunt/api/status", headers=headers()).status_code == 200
        assert client.get("/zhunt/api/themes", headers=headers()).json() == {"themes": []}


def status_models(client: TestClient) -> dict[str, dict]:
    body = client.get("/zhunt/api/status", headers=headers()).json()
    return {m["id"]: m for p in body["providers"] for m in p["models"]}


def test_status_shape_and_blocked_reasons() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        response = client.get("/zhunt/api/status", headers=headers())
        models = status_models(client)

    body = response.json()
    assert set(body) == {"version", "policy", "spend", "providers", "catalog"}
    assert body["version"]
    assert body["policy"] == {"allow_paid": False, "allow_training": False, "premium_reason": "pro only"}
    assert body["spend"]["spent"] == 0 and body["spend"]["cap"] == 5.0
    providers = {p["platform"]: p for p in body["providers"]}
    assert set(providers) == {"alpha", "beta", "gamma", "delta", "zeta", "epsilon"}
    assert providers["alpha"] == {
        "platform": "alpha",
        "name": "Fake",
        "key_set": True,
        "key_env": "FAKE_API_KEY",
        "key_url": "",
        "free_note": "",
        "keyless": False,
        "models": providers["alpha"]["models"],
    }
    assert providers["zeta"]["key_set"] is False
    assert set(models["alpha/coder"]) == {
        "id", "tiers", "free", "may_train_on_prompts", "allowed", "blocked_reason",
        "headroom", "cooldown_remaining", "reliability", "limits",
    }
    assert models["alpha/coder"]["tiers"] == ["chat", "coding"]
    assert models["alpha/coder"]["limits"] == {"rpm": None, "rpd": 3, "tpm": None, "tpd": None}
    assert (models["alpha/coder"]["allowed"], models["alpha/coder"]["blocked_reason"]) == (True, None)
    assert models["alpha/coder"]["headroom"] == 1.0
    assert models["alpha/coder"]["cooldown_remaining"] == 0
    assert models["alpha/coder"]["reliability"] == 0.5
    assert models["beta/trainer"]["may_train_on_prompts"] is True
    assert models["beta/trainer"]["blocked_reason"] == "may train on prompts"
    assert models["gamma/paid-chat"]["free"] is False
    assert models["gamma/paid-chat"]["blocked_reason"] == "pro only"
    assert models["zeta/z1"]["blocked_reason"] == "no key"
    assert not models["zeta/z1"]["allowed"]


def test_status_follows_the_policy_flags() -> None:
    env = {**ENV, "ZHUNT_ALLOW_TRAINING": "1", "ZHUNT_ALLOW_PAID": "1"}
    with workspace(AsyncMock(), env=env, plan=PRO) as (client, _):
        body = client.get("/zhunt/api/status", headers=headers()).json()
        models = status_models(client)

    assert body["policy"] == {"allow_paid": True, "allow_training": True, "premium_reason": None}
    assert models["beta/trainer"]["allowed"] and models["gamma/paid-chat"]["allowed"]


def test_status_reports_no_key_before_policy() -> None:
    with workspace(AsyncMock(), env={}) as (client, _):
        models = status_models(client)

    assert {m["blocked_reason"] for m in models.values()} == {"no key"}


def test_status_never_contains_a_key_value() -> None:
    env = {**ENV, "ZETA_API_KEY": ZETA_KEY}
    with workspace(AsyncMock(return_value=ok()), env=env) as (client, _):
        chat(client)
        text = client.get("/zhunt/api/status", headers=headers()).text
        text += client.get("/zhunt/api/decisions", headers=headers()).text
        text += compare(client, ["zeta/z1", "alpha/coder"]).text

    assert not any(secret in text for secret in SECRETS)
    assert "FAKE_API_KEY" in text  # the variable name is fine, the value is not


def test_status_reflects_live_quota_cooldown_and_reliability() -> None:
    call = AsyncMock(side_effect=[provider_error(retry_after=90), ok()])
    with workspace(call, env=ENV) as (client, _):
        chat(client)
        failed = call.await_args_list[0].kwargs["model"]
        served = call.await_args_list[1].kwargs["model"]
        models = status_models(client)

    assert models[failed]["cooldown_remaining"] == pytest.approx(90, abs=2)
    assert models[failed]["reliability"] < 0.5 < models[served]["reliability"]
    capped = next(m for m in models.values() if m["id"] == served)
    assert capped["headroom"] < 1.0  # one of rpm 5 / rpd 3 is spent


# -- decisions -------------------------------------------------------------


def decisions(client: TestClient, query: str = "") -> list[dict]:
    response = client.get(f"/zhunt/api/decisions{query}", headers=headers())
    assert response.status_code == 200
    return response.json()["decisions"]


def test_decisions_are_newest_first_with_session_filter_and_limit() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}, data=solo_catalog()) as (client, _):
        for session in ("ui-a", "ui-b", "ui-a"):
            assert chat(client, session).status_code == 200
        everything = decisions(client)
        only_a = decisions(client, "?session=ui-a")
        limited = decisions(client, "?limit=2")
        nothing = decisions(client, "?session=ui-zzz")

    assert [d["session"] for d in everything] == ["ui-a", "ui-b", "ui-a"]
    stamps = [d["ts"] for d in everything]
    assert stamps == sorted(stamps, reverse=True)
    assert [d["session"] for d in only_a] == ["ui-a", "ui-a"]
    assert limited == everything[:2]
    assert nothing == []
    entry = everything[0]
    assert set(entry) == {
        "ts", "session", "requested_alias", "tier", "model", "degraded", "attempt",
        "success", "failure", "input_tokens", "output_tokens", "latency_ms",
    }
    assert entry["requested_alias"] == "zhunt-auto"
    assert (entry["tier"], entry["model"]) == ("chat", "zeta/only")
    assert (entry["attempt"], entry["success"], entry["failure"], entry["degraded"]) == (1, True, None, False)
    assert isinstance(entry["latency_ms"], int) and entry["latency_ms"] >= 0


def test_decisions_use_reported_token_usage() -> None:
    response = ok()
    response["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    with workspace(AsyncMock(return_value=response), env={"ZETA_API_KEY": ZETA_KEY}, data=solo_catalog()) as (client, _):
        chat(client, "ui-t")
        entry = decisions(client)[0]

    assert (entry["input_tokens"], entry["output_tokens"]) == (11, 7)


def test_requests_without_a_session_header_fall_back_to_the_routing_session_key() -> None:
    with workspace(AsyncMock(return_value=ok()), env={"ZETA_API_KEY": ZETA_KEY}, data=solo_catalog()) as (client, _):
        chat(client)
        (entry,) = decisions(client)
        filtered = decisions(client, f"?session={entry['session']}")

    assert entry["session"].startswith("fallback:")
    assert filtered == [entry]


def test_a_failover_is_its_own_entry_followed_by_the_next_attempt() -> None:
    call = AsyncMock(side_effect=[provider_error(retry_after=7), ok()])
    with workspace(call, env=ENV) as (client, _):
        assert chat(client, "ui-f").status_code == 200
        entries = decisions(client, "?session=ui-f")

    newest, oldest = entries
    assert (oldest["attempt"], oldest["success"], oldest["failure"]) == (1, False, "rate-limit")
    assert (newest["attempt"], newest["success"], newest["failure"]) == (2, True, None)
    assert oldest["model"] != newest["model"]
    assert oldest["model"] == call.await_args_list[0].kwargs["model"]


def test_failure_label_distinguishes_client_errors_and_truncation() -> None:
    bad = provider_error(cls=llm.BadRequestError, status=400)
    with workspace(AsyncMock(side_effect=[bad]), env=ENV) as (client, _):
        assert chat(client, "ui-bad").status_code == 400
        (entry,) = decisions(client)
    assert (entry["success"], entry["failure"]) == (False, "client-error")

    cut = ok("cut", finish_reason="length")
    with workspace(AsyncMock(side_effect=[cut, ok("full")]), env=ENV) as (client, _):
        chat(client, "ui-cut")
        newest, oldest = decisions(client)
    assert (oldest["success"], oldest["failure"]) == (False, "truncation")
    assert (newest["attempt"], newest["success"]) == (2, True)


def test_an_exhausted_tier_is_recorded_as_degraded() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"FAKE_API_KEY": KEY}) as (client, _):
        for _ in range(3):
            assert chat(client, "ui-d", {**CHAT, "model": "zhunt-reasoning"}).status_code == 200
        entries = decisions(client)

    # delta/thinker is the only reasoning model and allows 2 requests a day
    assert [e["degraded"] for e in entries] == [True, False, False]
    assert [e["tier"] for e in entries][1:] == ["reasoning", "reasoning"]
    assert entries[0]["tier"] != "reasoning"
    assert all(e["requested_alias"] == "zhunt-reasoning" for e in entries)


def test_the_log_keeps_only_the_last_200_requests() -> None:
    with workspace(AsyncMock(return_value=ok()), env={"ZETA_API_KEY": ZETA_KEY}, data=solo_catalog()) as (client, _):
        for n in range(205):
            assert chat(client, f"s{n}").status_code == 200
        everything = decisions(client, "?limit=200")
        oldest_kept = everything[-1]["session"]
        first_session = decisions(client, "?session=s0")

    assert len(everything) == 200
    assert everything[0]["session"] == "s204" and oldest_kept == "s5"
    assert first_session == []


@pytest.mark.parametrize("query", ["?limit=0", "?limit=201", "?limit=abc"])
def test_decisions_reject_a_bad_limit(query: str) -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        assert client.get(f"/zhunt/api/decisions{query}", headers=headers()).status_code == 422


# -- compare ---------------------------------------------------------------


def test_compare_runs_models_concurrently_with_each_providers_own_key() -> None:
    in_flight = peak = 0

    async def slow(**kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return ok(f"answer from {kwargs['model']}")

    call = AsyncMock(side_effect=slow)
    env = {**ENV, "ZETA_API_KEY": ZETA_KEY}
    with workspace(call, env=env) as (client, hook):
        response = compare(client, ["alpha/coder", "zeta/z1"], "what is 2+2?")
        by_model = results(response)
        decision_count = len(decisions(client))
        served = hook.reliability.mean("zeta/z1")

    assert peak == 2
    assert set(by_model) == {"alpha/coder", "zeta/z1"}
    assert [r["model"] for r in response.json()["results"]] == ["alpha/coder", "zeta/z1"]
    assert by_model["zeta/z1"] == {
        "model": "zeta/z1", "ok": True, "text": "answer from zeta/z1", "error": None,
        "latency_ms": by_model["zeta/z1"]["latency_ms"], "input_tokens": None, "output_tokens": None,
    }
    assert by_model["zeta/z1"]["latency_ms"] >= 40
    keys = {c.kwargs["model"]: c.kwargs["api_key"] for c in call.await_args_list}
    assert keys == {"alpha/coder": KEY, "zeta/z1": ZETA_KEY}
    assert call.await_args_list[0].kwargs["messages"] == [{"role": "user", "content": "what is 2+2?"}]
    assert decision_count == 0  # compare bypasses routing, so it is not a routing decision
    assert served > 0.5


def test_compare_records_usage_quota_and_reliability() -> None:
    response = ok("x")
    response["usage"] = {"prompt_tokens": 12, "completion_tokens": 80, "total_tokens": 92}
    with workspace(AsyncMock(return_value=response), env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        got = results(compare(client, ["zeta/z2"]))["zeta/z2"]
        models = status_models(client)

    assert (got["input_tokens"], got["output_tokens"]) == (12, 80)
    assert models["zeta/z2"]["headroom"] == 0.5  # one of rpd 2
    assert models["zeta/z2"]["reliability"] > 0.5


def test_compare_blocks_what_policy_or_keys_exclude_without_calling_the_provider() -> None:
    call = AsyncMock(return_value=ok("fine"))
    with workspace(call, env={**ENV, "ZETA_API_KEY": ZETA_KEY}) as (client, _):
        got = results(compare(client, ["alpha/coder", "beta/trainer", "gamma/paid-chat", "epsilon/e1"]))

    assert got["alpha/coder"]["ok"] is True
    for model_id, reason in [
        ("beta/trainer", "may train on prompts"),
        ("gamma/paid-chat", "pro only"),
        ("epsilon/e1", "no key"),
    ]:
        assert got[model_id] == {
            "model": model_id, "ok": False, "text": None, "error": f"blocked: {reason}",
            "latency_ms": None, "input_tokens": None, "output_tokens": None,
        }
    assert [c.kwargs["model"] for c in call.await_args_list] == ["alpha/coder"]


def test_compare_allows_a_training_model_once_the_user_opted_in() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={**ENV, "ZHUNT_ALLOW_TRAINING": "1"}) as (client, _):
        got = results(compare(client, ["beta/trainer"]))

    assert got["beta/trainer"]["ok"] is True


def test_compare_blocks_a_provider_whose_key_vanished_from_the_environment() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env=ENV) as (client, _):
        with patch.dict(os.environ, {"FAKE_API_KEY": ""}):
            got = results(compare(client, ["alpha/coder"]))

    assert got["alpha/coder"]["error"] == "blocked: no key"
    call.assert_not_awaited()


def test_compare_honours_quota() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        outcomes = [results(compare(client, ["zeta/z2"]))["zeta/z2"] for _ in range(3)]

    assert [o["ok"] for o in outcomes] == [True, True, False]
    assert outcomes[2]["error"] == "blocked: over quota"
    assert call.await_count == 2


def test_compare_failure_benches_the_model_and_the_next_compare_is_blocked() -> None:
    call = AsyncMock(side_effect=[provider_error(retry_after=300)])
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, hook):
        first = results(compare(client, ["zeta/z1"]))["zeta/z1"]
        second = results(compare(client, ["zeta/z1"]))["zeta/z1"]
        cooldown = hook.quota.cooldown_remaining(("zeta/z1", "default"))
        reliability = hook.reliability.mean("zeta/z1")
        listed = status_models(client)["zeta/z1"]

    assert first["ok"] is False and first["error"].startswith("429:")
    assert second["error"].startswith("blocked: cooling down")
    assert cooldown == pytest.approx(300, abs=2)  # retry-after above the 90 s floor
    assert reliability < 0.5
    assert listed["cooldown_remaining"] == pytest.approx(300, abs=2)
    assert call.await_count == 1


def test_compare_with_a_model_failing_while_another_succeeds() -> None:
    async def by_model(**kwargs):
        if kwargs["model"] == "zeta/z1":
            raise provider_error(cls=llm.InternalServerError, status=500)
        return ok("alpha says hi")

    call = AsyncMock(side_effect=by_model)
    with workspace(call, env={**ENV, "ZETA_API_KEY": ZETA_KEY}) as (client, hook):
        response = compare(client, ["zeta/z1", "alpha/coder"])
        got = results(response)

    assert got["alpha/coder"]["ok"] and got["alpha/coder"]["text"] == "alpha says hi"
    assert not got["zeta/z1"]["ok"] and got["zeta/z1"]["text"] is None
    assert got["zeta/z1"]["error"].startswith("500:")
    assert hook.quota.cooldown_remaining(("zeta/z1", "default")) > 0
    assert hook.quota.cooldown_remaining(("alpha/coder", "default")) == 0


def test_a_client_error_in_compare_does_not_bench_the_model() -> None:
    call = AsyncMock(side_effect=[provider_error(cls=llm.BadRequestError, status=400)])
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, hook):
        got = results(compare(client, ["zeta/z1"]))["zeta/z1"]
        cooldown = hook.quota.cooldown_remaining(("zeta/z1", "default"))

    assert got["ok"] is False and cooldown == 0


def test_a_rejected_key_in_compare_benches_every_model_of_that_provider() -> None:
    call = AsyncMock(side_effect=[provider_error(cls=llm.AuthenticationError, status=401)])
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, hook):
        results(compare(client, ["zeta/z1"]))
        sibling = hook.quota.cooldown_remaining(("zeta/z2", "default"))

    assert sibling > 0


def test_compare_errors_never_echo_the_key() -> None:
    boom = RuntimeError(f"Incorrect API key provided: {ZETA_KEY}. Visit the dashboard." + "x" * 2000)
    with workspace(AsyncMock(side_effect=[boom]), env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        response = compare(client, ["zeta/z1"])
        error = results(response)["zeta/z1"]["error"]

    assert ZETA_KEY not in response.text
    assert error.startswith("Incorrect API key provided: ***")
    assert len(error) <= 300


def test_compare_collapses_duplicate_model_ids() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        response = compare(client, ["zeta/z1", "zeta/z1"])

    assert len(response.json()["results"]) == 1
    assert call.await_count == 1


def test_compare_unknown_models_are_a_404_naming_them_and_nothing_is_called() -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        response = compare(client, ["zeta/z1", "nope/one", "nope/two"])

    assert response.status_code == 404
    assert "nope/one" in response.text and "nope/two" in response.text
    call.assert_not_awaited()


def test_compare_accepts_exactly_four_models_and_rejects_five() -> None:
    call = AsyncMock(return_value=ok())
    four = ["alpha/chat-fast", "alpha/coder", "zeta/z1", "zeta/z2"]
    with workspace(call, env={**ENV, "ZETA_API_KEY": ZETA_KEY}) as (client, _):
        accepted = compare(client, four)
        rejected = compare(client, [*four, "epsilon/e1"])

    assert accepted.status_code == 200 and len(accepted.json()["results"]) == 4
    assert rejected.status_code == 400
    assert call.await_count == 4  # nothing ran for the rejected one


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"{}",
        b'{"messages": [], "models": ["zeta/z1"]}',
        b'{"messages": "hi", "models": ["zeta/z1"]}',
        b'{"messages": [{"role": "user"}], "models": ["zeta/z1"]}',
        b'{"messages": ["hi"], "models": ["zeta/z1"]}',
        b'{"messages": [{"role": "user", "content": "x"}], "models": []}',
        b'{"messages": [{"role": "user", "content": "x"}], "models": "zeta/z1"}',
        b'{"messages": [{"role": "user", "content": "x"}], "models": [1]}',
    ],
)
def test_compare_rejects_an_invalid_body(body: bytes) -> None:
    call = AsyncMock(return_value=ok())
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, _):
        response = client.post("/zhunt/api/compare", headers=headers(), content=body)

    assert response.status_code == 400
    call.assert_not_awaited()


# -- layouts ---------------------------------------------------------------


def layout_url(name: str) -> str:
    return f"/zhunt/api/layouts/{name}"


def test_layout_crud_roundtrip() -> None:
    layout = {"tiles": [{"kind": "chat"}, {"kind": "compare"}], "split": 0.4}
    with workspace(AsyncMock(), env=ENV) as (client, _):
        assert client.get("/zhunt/api/layouts", headers=headers()).json() == {"layouts": []}
        assert client.put(layout_url("coding"), headers=headers(), json=layout).status_code == 200
        assert client.put(layout_url("default"), headers=headers(), json={"a": 1}).status_code == 200
        assert client.get("/zhunt/api/layouts", headers=headers()).json() == {"layouts": ["coding", "default"]}
        assert client.get(layout_url("coding"), headers=headers()).json() == layout
        client.put(layout_url("coding"), headers=headers(), json={"replaced": True})
        assert client.get(layout_url("coding"), headers=headers()).json() == {"replaced": True}
        assert client.delete(layout_url("coding"), headers=headers()).status_code == 200
        assert client.get(layout_url("coding"), headers=headers()).status_code == 404
        assert client.delete(layout_url("coding"), headers=headers()).status_code == 404
        assert client.get("/zhunt/api/layouts", headers=headers()).json() == {"layouts": ["default"]}
        assert client.get(layout_url("missing"), headers=headers()).status_code == 404


def test_layout_files_are_private_atomic_and_hand_editable() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        client.put(layout_url("mine"), headers=headers(), json={"a": 1})
        client.put(layout_url("mine"), headers=headers(), json={"a": 2})
        folder = client.dirs.layouts
        mode = stat.S_IMODE((folder / "mine.json").stat().st_mode)
        folder_mode = stat.S_IMODE(folder.stat().st_mode)
        leftovers = sorted(p.name for p in folder.iterdir())
        stored = json.loads((folder / "mine.json").read_text())
        (folder / "edited.json").write_text('{"by": "hand"}')
        (folder / "Bad Name.json").write_text("{}")
        (folder / "notes.txt").write_text("x")
        listed = client.get("/zhunt/api/layouts", headers=headers()).json()["layouts"]
        by_hand = client.get(layout_url("edited"), headers=headers()).json()

    assert mode == 0o600
    assert folder_mode == 0o700
    assert leftovers == ["mine.json"]  # no temp files survive
    assert stored == {"a": 2}
    assert listed == ["edited", "mine"]  # names that could not be addressed are not listed
    assert by_hand == {"by": "hand"}


def test_a_failed_write_leaves_the_old_layout_and_no_temp_file() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        client.put(layout_url("keep"), headers=headers(), json={"v": 1})
        with patch("zhunt.workspace_api.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                client.put(layout_url("keep"), headers=headers(), json={"v": 2})
        assert client.get(layout_url("keep"), headers=headers()).json() == {"v": 1}
        assert sorted(p.name for p in client.dirs.layouts.iterdir()) == ["keep.json"]


@pytest.mark.parametrize(
    "name",
    ["A", "Default", "-x", "_x", "a.json", "a.b", "x" * 41, "a b", ".hidden", "name%0a", "caf%C3%A9", "%2e%2e", ".."],
)
def test_bad_layout_names_are_rejected_for_every_method(name: str) -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        for method in ("GET", "PUT", "DELETE"):
            response = client.request(method, layout_url(name), headers=headers(), json={"a": 1})
            assert response.status_code in (400, 404), (method, name)
        if name not in {".."}:  # httpx normalises a bare ".." away before it is sent
            assert client.put(layout_url(name), headers=headers(), json={}).status_code == 400
        assert not client.dirs.layouts.exists()


@pytest.mark.parametrize(
    "name", ["..%2f..%2fetc%2fpasswd", "%2e%2e%2f%2e%2e%2fescape", "a%2fb", "a%5cb", "..%5c..%5cx", "%2fetc%2fpasswd"]
)
def test_path_traversal_attempts_never_reach_the_filesystem(name: str) -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        outside = client.dirs.layouts.parent
        responses = [
            client.request(method, layout_url(name), headers=headers(), json={"a": 1})
            for method in ("GET", "PUT", "DELETE")
        ]
        assert all(r.status_code in (400, 404) for r in responses)
        assert not client.dirs.layouts.exists()
        assert not list(outside.rglob("*.json"))
        assert not list(client.dirs.layouts.parent.parent.rglob("escape*"))


def test_the_longest_and_shortest_valid_names_are_accepted() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        for name in ("a", "0", "a-b_c", "x" * 40):
            assert client.put(layout_url(name), headers=headers(), json={}).status_code == 200, name


def test_layout_size_limit_is_64kb() -> None:
    def body_of(size: int) -> bytes:
        base = json.dumps({"pad": ""})
        return json.dumps({"pad": "x" * (size - len(base))}).encode()

    exact, over = body_of(MAX_LAYOUT_BYTES), body_of(MAX_LAYOUT_BYTES + 1)
    assert (len(exact), len(over)) == (MAX_LAYOUT_BYTES, MAX_LAYOUT_BYTES + 1)
    with workspace(AsyncMock(), env=ENV) as (client, _):
        ok_put = client.put(layout_url("big"), headers=headers(), content=exact)
        too_big = client.put(layout_url("huge"), headers=headers(), content=over)

        def chunks():  # no Content-Length, so the cap has to hold while streaming
            for _ in range(10):
                yield b" " * 10_000

        streamed = client.put(layout_url("streamed"), headers=headers(), content=chunks())
        names = client.get("/zhunt/api/layouts", headers=headers()).json()["layouts"]

    assert ok_put.status_code == 200
    assert too_big.status_code == 413
    assert streamed.status_code == 413
    assert names == ["big"]


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"3", b"null", b"not json", b""])
def test_layout_body_must_be_a_json_object(body: bytes) -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        response = client.put(layout_url("x"), headers=headers(), content=body)
        exists = client.dirs.layouts.exists() and any(client.dirs.layouts.iterdir())

    assert response.status_code == 400
    assert not exists


def test_a_corrupt_layout_file_is_a_clear_error_not_a_crash() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        client.dirs.layouts.mkdir(parents=True)
        (client.dirs.layouts / "broken.json").write_text("{nope")
        response = client.get(layout_url("broken"), headers=headers())

    assert response.status_code == 500
    assert "not valid JSON" in response.json()["detail"]


# -- themes ----------------------------------------------------------------


def test_builtin_themes_are_listed_and_served_as_css() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        listed = client.get("/zhunt/api/themes", headers=headers()).json()
        dark = client.get("/zhunt/api/themes/dark", headers=headers())

    assert listed == {"themes": [{"name": "dark", "builtin": True}, {"name": "light", "builtin": True}]}
    assert dark.status_code == 200
    assert dark.headers["content-type"].startswith("text/css")
    assert dark.text == ":root{--bg:#000}"


def test_a_user_theme_overrides_the_builtin_of_the_same_name() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        client.dirs.themes.mkdir(parents=True)
        (client.dirs.themes / "dark.css").write_text(":root{--bg:#123}")
        (client.dirs.themes / "solar.css").write_text(":root{--bg:#fd0}")
        (client.dirs.themes / "Bad Name.css").write_text("x")
        listed = client.get("/zhunt/api/themes", headers=headers()).json()["themes"]
        dark = client.get("/zhunt/api/themes/dark", headers=headers()).text
        solar = client.get("/zhunt/api/themes/solar", headers=headers()).text
        light = client.get("/zhunt/api/themes/light", headers=headers()).text

    assert [t["name"] for t in listed] == ["dark", "light", "solar"]  # dark appears once
    assert {t["name"]: t["builtin"] for t in listed} == {"dark": False, "light": True, "solar": False}
    assert dark == ":root{--bg:#123}"
    assert solar == ":root{--bg:#fd0}"
    assert light == ":root{--bg:#fff}"


def test_missing_or_invalid_theme_names() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        assert client.get("/zhunt/api/themes/nope", headers=headers()).status_code == 404
        for name in ("Dark", "-x", "a.css", "x" * 41, "%2e%2e", "..%2f..%2fcatalog"):
            assert client.get(f"/zhunt/api/themes/{name}", headers=headers()).status_code in (400, 404), name
        assert client.get("/zhunt/api/themes/Dark", headers=headers()).status_code == 400


PRO = Plan(name="pro", features=FEATURES["pro"], expires_at=2e9)


def test_every_shipped_theme_is_free() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        (client.dirs.static / "css" / "themes" / "nord.css").write_text(":root{--bg:#2e3440}")
        listed = {t["name"]: t for t in client.get("/zhunt/api/themes", headers=headers()).json()["themes"]}
        served = client.get("/zhunt/api/themes/nord", headers=headers())
        plan = client.get("/zhunt/api/plan", headers=headers()).json()

    assert listed["nord"] == {"name": "nord", "builtin": True}
    assert served.status_code == 200 and served.text == ":root{--bg:#2e3440}"
    assert plan["plan"] == "free" and plan["features"] == [] and plan["upgrade_url"].startswith("https://")


def test_plan_offers_help_to_everyone_and_priority_support_to_pro() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        free = client.get("/zhunt/api/plan", headers=headers()).json()
    with workspace(AsyncMock(), env=ENV, plan=PRO) as (client, _):
        pro = client.get("/zhunt/api/plan", headers=headers()).json()

    for plan in (free, pro):
        assert plan["setup_guide_url"] == "https://zhunt.kindredwildcat.com/setup.html"
        assert "@" in plan["support_email"]
    assert "priority_support" in pro["features"] and "priority_support" not in free["features"]
    assert "one working day" in pro["feature_labels"]["priority_support"]


def test_premium_needs_pro_and_the_opt_in_and_stops_at_the_cap() -> None:
    opted_in = {**ENV, "ZHUNT_ALLOW_PAID": "1"}
    with workspace(AsyncMock(), env=ENV, plan=PRO) as (client, _):
        not_opted = client.get("/zhunt/api/status", headers=headers()).json()["policy"]
    with workspace(AsyncMock(), env=opted_in) as (client, _):
        free = client.get("/zhunt/api/status", headers=headers()).json()["policy"]
    with workspace(AsyncMock(), env={**opted_in, "ZHUNT_MONTHLY_CAP": "1"}, plan=PRO) as (client, hook):
        hook.spend.add("gamma/paid-chat", 1.25)
        capped = client.get("/zhunt/api/status", headers=headers()).json()
        paid = status_models(client)["gamma/paid-chat"]

    assert not_opted == {"allow_paid": False, "allow_training": False, "premium_reason": "premium off"}
    assert free["premium_reason"] == "pro only"  # the opt-in alone does not unlock it
    assert capped["policy"]["premium_reason"] == "monthly cap reached"
    assert capped["spend"]["spent"] == 1.25 and capped["spend"]["cap"] == 1.0
    assert paid["blocked_reason"] == "monthly cap reached"


def test_a_key_pasted_in_the_connect_window_is_saved_privately_and_used_at_once() -> None:
    import stat

    with workspace(AsyncMock(), env={}, data=solo_catalog()) as (client, hook):
        before = status_models(client)["zeta/only"]["blocked_reason"]
        saved = client.post("/zhunt/api/keys", headers=headers(), json={"platform": "zeta", "key": "zk-live-1234567890"})
        after = status_models(client)["zeta/only"]
        env_file = client.dirs.env_file
        mode = stat.S_IMODE(env_file.stat().st_mode)
        content = env_file.read_text()
        removed = client.post("/zhunt/api/keys", headers=headers(), json={"platform": "zeta", "key": "bad key with spaces"})
        unknown = client.post("/zhunt/api/keys", headers=headers(), json={"platform": "nope", "key": "zk-live-1234567890"})
        deleted = client.delete("/zhunt/api/keys/zeta", headers=headers())
        gone = status_models(client)["zeta/only"]["blocked_reason"]

    assert before == "no key"
    assert saved.status_code == 200 and "zk-live" not in saved.text  # never echoed back
    assert after["allowed"] is True
    assert mode == 0o600 and 'ZETA_API_KEY="zk-live-1234567890"' in content
    assert removed.status_code == 400 and unknown.status_code == 404
    assert deleted.status_code == 200 and gone == "no key"


def test_the_key_endpoints_need_the_master_key() -> None:
    with workspace(AsyncMock(), env={}, data=solo_catalog()) as (client, _):
        response = client.post("/zhunt/api/keys", json={"platform": "zeta", "key": "zk-live-1234567890"})
        written = client.dirs.env_file.exists()

    assert response.status_code == 401 and not written


def test_voice_input_goes_to_groq_whisper_with_the_users_key() -> None:
    transcription = AsyncMock(return_value=SimpleNamespace(text=" write a regex for postcodes "))
    with patch("zhunt.llm.atranscription", transcription), workspace(AsyncMock(), env={**ENV, "GROQ_API_KEY": "gsk-user-123456"}) as (client, _):
        ok = client.post("/zhunt/api/transcribe", headers=headers(**{"content-type": "audio/webm"}), content=b"\x1aE\xdf\xa3fake")
        wrong = client.post("/zhunt/api/transcribe", headers=headers(**{"content-type": "text/plain"}), content=b"hi")
        empty = client.post("/zhunt/api/transcribe", headers=headers(**{"content-type": "audio/webm"}), content=b"")

    assert ok.status_code == 200 and ok.json() == {"text": "write a regex for postcodes"}
    kwargs = transcription.await_args.kwargs
    assert kwargs["model"] == "groq/whisper-large-v3-turbo" and kwargs["api_key"] == "gsk-user-123456"
    assert kwargs["file"][0] == "speech.webm"
    assert wrong.status_code == 415 and empty.status_code == 400


def test_voice_input_without_a_groq_key_says_how_to_fix_it_and_errors_hide_the_key() -> None:
    failing = AsyncMock(side_effect=RuntimeError("bad key gsk-user-123456 rejected"))
    with workspace(AsyncMock(), env={}) as (client, _):
        missing = client.post("/zhunt/api/transcribe", headers=headers(**{"content-type": "audio/webm"}), content=b"x")
    with patch("zhunt.llm.atranscription", failing), workspace(AsyncMock(), env={**ENV, "GROQ_API_KEY": "gsk-user-123456"}) as (client, _):
        failed = client.post("/zhunt/api/transcribe", headers=headers(**{"content-type": "audio/webm"}), content=b"x")

    assert missing.status_code == 409 and "Groq" in missing.json()["detail"]
    assert failed.status_code == 502 and "gsk-user-123456" not in failed.text


def test_the_training_switch_unlocks_may_train_providers_and_is_saved() -> None:
    with workspace(AsyncMock(), env=ENV) as (client, _):
        before = status_models(client)["beta/trainer"]["blocked_reason"]
        on = client.put("/zhunt/api/policy/training", headers=headers(), json={"allow": True})
        after = status_models(client)["beta/trainer"]["allowed"]
        saved = client.dirs.env_file.read_text()
        bad = client.put("/zhunt/api/policy/training", headers=headers(), json={"allow": "yes"})
        client.put("/zhunt/api/policy/training", headers=headers(), json={"allow": False})
        off = status_models(client)["beta/trainer"]["blocked_reason"]

    assert before == "may train on prompts" and on.json() == {"allow_training": True}
    assert after is True and 'ZHUNT_ALLOW_TRAINING="1"' in saved
    assert bad.status_code == 400 and off == "may train on prompts"


def test_bots_are_plain_json_files_that_round_trip_and_are_validated() -> None:
    import stat

    bot = {"name": "Researcher", "instructions": "Find facts.", "alias": "zhunt-reasoning", "color": "#7AA2F7"}
    with workspace(AsyncMock(), env=ENV) as (client, _):
        saved = client.put("/zhunt/api/bots/researcher", headers=headers(), json=bot)
        listed = client.get("/zhunt/api/bots", headers=headers()).json()
        path = client.dirs.bots / "researcher.json"
        mode = stat.S_IMODE(path.stat().st_mode)
        (client.dirs.bots / "broken.json").write_text("{not json")
        still = client.get("/zhunt/api/bots", headers=headers()).json()
        bad_alias = client.put("/zhunt/api/bots/x", headers=headers(), json={**bot, "alias": "gpt-5"})
        bad_handle = client.put("/zhunt/api/bots/Bad%20Name", headers=headers(), json=bot)
        no_name = client.put("/zhunt/api/bots/y", headers=headers(), json={**bot, "name": " "})
        deleted = client.delete("/zhunt/api/bots/researcher", headers=headers())
        after = client.get("/zhunt/api/bots", headers=headers()).json()

    assert saved.json() == {"handle": "researcher", "name": "Researcher", "instructions": "Find facts.", "alias": "zhunt-reasoning", "color": "#7aa2f7"}
    assert listed == {"bots": [{**saved.json(), "avatar": False}]} and mode == 0o600
    assert [b["handle"] for b in still["bots"]] == ["researcher"]  # a broken hand edit is skipped
    assert bad_alias.status_code == 400 and bad_handle.status_code == 400 and no_name.status_code == 400
    assert deleted.status_code == 200 and after == {"bots": []}


ACCOUNT = "0123456789abcdef0123456789abcdef"
CF_TOKEN = "cf-token-abcdefghijklmnopqrstuvwxyz"


def cloudflare_reply(status=200, image=b"\xff\xd8\xffjpeg"):
    import base64 as b64

    body = {"result": {"image": b64.b64encode(image).decode()}, "success": True} if status == 200 else {"errors": [{"message": f"bad {CF_TOKEN}"}]}
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.cloudflare.com/x"))


def test_ai_pictures_need_cloudflare_connected_and_use_the_users_account() -> None:
    post = AsyncMock(return_value=cloudflare_reply())
    with patch("httpx.AsyncClient.post", post), workspace(AsyncMock(), env=ENV) as (client, _):
        before = client.post("/zhunt/api/images", headers=headers(), json={"prompt": "a fox"})
        bad = client.put("/zhunt/api/images/connect", headers=headers(), json={"account_id": "nope", "token": CF_TOKEN})
        ok = client.put("/zhunt/api/images/connect", headers=headers(), json={"account_id": ACCOUNT, "token": CF_TOKEN})
        status = client.get("/zhunt/api/images/status", headers=headers()).json()
        made = client.post("/zhunt/api/images", headers=headers(), json={"prompt": "a friendly fox avatar"})
        saved = client.dirs.env_file.read_text()

    assert before.status_code == 409 and "Connect" in before.json()["detail"]
    assert bad.status_code == 400 and ok.json() == {"connected": True} and status["connected"] is True
    assert made.status_code == 200 and made.json()["image"].startswith("data:image/jpeg;base64,")
    url = post.await_args.args[0]
    assert ACCOUNT in url and url.endswith("@cf/black-forest-labs/flux-1-schnell")
    assert post.await_args.kwargs["headers"]["Authorization"] == f"Bearer {CF_TOKEN}"
    assert f'CLOUDFLARE_API_TOKEN="{CF_TOKEN}"' in saved and CF_TOKEN not in made.text


def test_cloudflare_errors_never_show_the_token() -> None:
    env = {**ENV, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_API_TOKEN": CF_TOKEN}
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=cloudflare_reply(400))), workspace(AsyncMock(), env=env) as (client, _):
        failed = client.post("/zhunt/api/images", headers=headers(), json={"prompt": "x"})
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=cloudflare_reply(429))), workspace(AsyncMock(), env=env) as (client, _):
        limited = client.post("/zhunt/api/images", headers=headers(), json={"prompt": "x"})

    assert failed.status_code == 502 and CF_TOKEN not in failed.text
    assert limited.status_code == 429


def test_bot_pictures_round_trip_and_are_checked() -> None:
    import base64 as b64

    png = "data:image/png;base64," + b64.b64encode(b"\x89PNG fake").decode()
    bot = {"name": "Writer", "instructions": "", "alias": "zhunt-chat", "color": "#9ece6a"}
    with workspace(AsyncMock(), env=ENV) as (client, _):
        client.put("/zhunt/api/bots/writer", headers=headers(), json=bot)
        missing = client.get("/zhunt/api/bots/writer/avatar", headers=headers())
        put = client.put("/zhunt/api/bots/writer/avatar", headers=headers(), json={"image": png})
        listed = client.get("/zhunt/api/bots", headers=headers()).json()["bots"][0]["avatar"]
        got = client.get("/zhunt/api/bots/writer/avatar", headers=headers()).json()["image"]
        svg = client.put("/zhunt/api/bots/writer/avatar", headers=headers(), json={"image": "data:image/svg+xml;base64,PHN2Zz4="})
        client.delete("/zhunt/api/bots/writer/avatar", headers=headers())
        gone = client.get("/zhunt/api/bots/writer/avatar", headers=headers())

    assert missing.status_code == 404 and put.json() == {"handle": "writer", "avatar": True}
    assert listed is True and got == png
    assert svg.status_code == 400  # SVG can carry script: not accepted
    assert gone.status_code == 404


def test_a_personal_wallpaper_round_trips_and_clears() -> None:
    import base64 as b64

    jpeg = "data:image/jpeg;base64," + b64.b64encode(b"\xff\xd8\xff wallpaper").decode()
    with workspace(AsyncMock(), env=ENV) as (client, _):
        none = client.get("/zhunt/api/wallpaper", headers=headers())
        put = client.put("/zhunt/api/wallpaper", headers=headers(), json={"image": jpeg})
        got = client.get("/zhunt/api/wallpaper", headers=headers()).json()["image"]
        bad = client.put("/zhunt/api/wallpaper", headers=headers(), json={"image": "data:text/html;base64,PGI+"})
        client.delete("/zhunt/api/wallpaper", headers=headers())
        gone = client.get("/zhunt/api/wallpaper", headers=headers())

    assert none.status_code == 404 and put.json() == {"wallpaper": True} and got == jpeg
    assert bad.status_code == 400 and gone.status_code == 404


def _tool_reply(name, args):
    import json as _json

    return llm.ModelResponse({"id": "r", "model": "x", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": name, "arguments": _json.dumps(args)}}]}}]})


def _text_reply(text):
    return llm.ModelResponse({"id": "r", "model": "x", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})


def test_an_agent_task_runs_end_to_end_through_zhunts_own_routing(tmp_path):
    import time as _time

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "receipt.txt").write_text("coffee 3.20")
    call = AsyncMock(side_effect=[_tool_reply("list_files", {}), _text_reply("You have one file: receipt.txt.")])
    with workspace(call, env=ENV) as (client, _):
        refused = client.put("/zhunt/api/agent/folders", headers=headers(), json={"folders": ["/"]})
        shared_ok = client.put("/zhunt/api/agent/folders", headers=headers(), json={"folders": [str(shared)]})
        task = client.post("/zhunt/api/agent/tasks", headers=headers(), json={"goal": "What files do I have?"}).json()
        for _ in range(100):
            detail = client.get(f"/zhunt/api/agent/tasks/{task['id']}", headers=headers()).json()
            if detail["state"] not in ("running", "waiting"):
                break
            _time.sleep(0.05)
        state = client.get("/zhunt/api/agent", headers=headers()).json()
        bad = client.post("/zhunt/api/agent/tasks", headers=headers(), json={"goal": "x", "tools": ["rm_rf"]})

    assert refused.status_code == 400 and shared_ok.json()["folders"] == [str(shared.resolve())]
    assert detail["state"] == "done" and detail["answer"] == "You have one file: receipt.txt."
    tool = [e for e in detail["events"] if e["kind"] == "tool"][0]
    assert tool["name"] == "list_files" and "receipt.txt" in tool["result"]
    # each step went through zhunt's routing to a tool-capable model, with the tools offered
    first = call.await_args_list[0].kwargs
    assert {t["function"]["name"] for t in first["tools"]} >= {"web_search", "list_files", "write_file"}
    assert first["model"].endswith("coder")  # the only tool-capable free model in the test catalog
    assert state["tasks"][0]["id"] == task["id"] and bad.status_code == 400


def test_email_connects_only_after_a_real_login_and_never_reveals_the_password():
    from zhunt.agent.tools import ToolError

    body = {"provider": "gmail", "address": "me@gmail.com", "password": "abcd efgh ijkl mnop"}
    with patch("zhunt.agent.email_tools.check_login", side_effect=ToolError("the email server refused the login")), workspace(AsyncMock(), env=ENV) as (client, _):
        refused = client.put("/zhunt/api/email/connect", headers=headers(), json=body)
        nothing_saved = not client.dirs.env_file.exists() or "ZHUNT_EMAIL" not in client.dirs.env_file.read_text()
    with patch("zhunt.agent.email_tools.check_login", return_value=None), workspace(AsyncMock(), env=ENV) as (client, _):
        ok = client.put("/zhunt/api/email/connect", headers=headers(), json=body)
        status = client.get("/zhunt/api/email/status", headers=headers())
        tools = {t["name"] for t in client.get("/zhunt/api/agent", headers=headers()).json()["tools"]}
        saved = client.dirs.env_file.read_text()
        client.delete("/zhunt/api/email/connect", headers=headers())
        after = client.get("/zhunt/api/email/status", headers=headers()).json()

    assert refused.status_code == 400 and nothing_saved
    assert ok.json() == {"connected": True, "address": "me@gmail.com"}
    assert 'ZHUNT_EMAIL_PASSWORD="abcdefghijklmnop"' in saved and 'ZHUNT_EMAIL_IMAP="imap.gmail.com:993"' in saved
    assert "abcdefghijklmnop" not in status.text and status.json()["connected"] is True
    assert {"list_emails", "read_email", "draft_email", "send_email"} <= tools
    assert after["connected"] is False
