"""Workspace API (docs/WORKSPACE_API.md) driven through the real daemon app."""

import asyncio
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import litellm
import pytest
import yaml
from fastapi.testclient import TestClient
from litellm.proxy import proxy_server

from tests.catalog_fixtures import catalog_data, model, provider
from tests.test_engine import CHAT, ok, provider_error
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
    return {"Authorization": f"Bearer {key or proxy_server.master_key}", **extra}


@contextmanager
def workspace(call: AsyncMock, *, env: dict[str, str], data: dict | None = None, static=True, plan=None):
    """Free-tier daemon with the workspace mounted on temp dirs; litellm patched."""

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "catalog.yaml").write_text(yaml.safe_dump(data or catalog()))
        static_dir = root / "static"
        if static:
            (static_dir / "css" / "themes").mkdir(parents=True)
            (static_dir / "index.html").write_text("<h1>workspace</h1>")
            (static_dir / "css" / "themes" / "dark.css").write_text(":root{--bg:#000}")
            (static_dir / "css" / "themes" / "light.css").write_text(":root{--bg:#fff}")
        dirs = WorkspaceDirs(layouts=root / "home" / "layouts", themes=root / "home" / "themes", static=static_dir, env_file=root / "home" / "env")
        with patch.dict(os.environ, {**dict.fromkeys(ENV_NAMES, ""), **env}), patch(
            "litellm.acompletion", call
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
    assert set(body) == {"version", "policy", "spend", "providers"}
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
    response.usage = litellm.Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
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
    bad = provider_error(cls=litellm.BadRequestError, status=400)
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
    response.usage = litellm.Usage(prompt_tokens=12, completion_tokens=80, total_tokens=92)
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
            raise provider_error(cls=litellm.InternalServerError, status=500)
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
    call = AsyncMock(side_effect=[provider_error(cls=litellm.BadRequestError, status=400)])
    with workspace(call, env={"ZETA_API_KEY": ZETA_KEY}) as (client, hook):
        got = results(compare(client, ["zeta/z1"]))["zeta/z1"]
        cooldown = hook.quota.cooldown_remaining(("zeta/z1", "default"))

    assert got["ok"] is False and cooldown == 0


def test_a_rejected_key_in_compare_benches_every_model_of_that_provider() -> None:
    call = AsyncMock(side_effect=[provider_error(cls=litellm.AuthenticationError, status=401)])
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
