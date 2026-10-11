"""Regressions for bugs found by the independent reviews of the v2 branch."""

import os
import random
from unittest.mock import AsyncMock

import pytest

from tests.catalog_fixtures import FakeClock, catalog_data, model, provider
from tests.test_engine import CHAT, daemon, ok, post, provider_error
from zhunt import llm
from zhunt.brain import Tier
from zhunt.catalog import Catalog
from zhunt.engine import (
    _banned_request_params,
    _enforce_engine_routing,
    _retry_after,
    registry_from_catalog,
)
from zhunt.selection import Reliability

PROVIDER_KEY = "key-secret-123"
ENV = {"FAKE_API_KEY": PROVIDER_KEY}


# -- provider-key exfiltration through client-controlled routing fields ----------

HOSTILE_FIELDS = {
    "api_base": "http://attacker.invalid/steal",
    "base_url": "http://attacker.invalid/steal",
    "custom_llm_provider": "openai",
    "fallbacks": [{"model": "openai/x", "api_base": "http://attacker.invalid"}],
    "models": ["openai/x"],
    "extra_headers": {"authorization": "Bearer attacker"},
    "num_retries": 9,
    "max_retries": 9,
    "azure_ad_token": "t",
    "aws_bedrock_runtime_endpoint": "http://attacker.invalid",
    "langfuse_host": "http://attacker.invalid",
}


@pytest.mark.parametrize("field", sorted(HOSTILE_FIELDS))
def test_client_cannot_redirect_the_call_or_pick_its_route(field: str) -> None:
    call = AsyncMock(return_value=ok())
    body = {**CHAT, field: HOSTILE_FIELDS[field]}
    with daemon(call, env=ENV) as (client, _):
        response = post(client, body)

    assert response.status_code == 200
    kwargs = call.await_args.kwargs
    if field in ("num_retries", "max_retries"):
        assert kwargs[field] == 0  # the client's value is replaced, not honoured
    else:
        assert field not in kwargs
    assert kwargs["api_key"] == PROVIDER_KEY
    assert kwargs["model"].startswith("alpha/")
    assert kwargs["max_retries"] == 0 and kwargs["num_retries"] == 0


def test_hostile_fields_are_scrubbed_from_nested_metadata_too() -> None:
    data = {
        "model": "zhunt-auto",
        "extra_body": {"api_base": "http://attacker.invalid"},
        "metadata": {"api_base": "http://attacker.invalid", "keep": 1},
        "litellm_metadata": {"base_url": "http://attacker.invalid"},
    }

    _enforce_engine_routing(data, "acompletion")

    assert "extra_body" not in data
    assert data["metadata"] == {"keep": 1}
    assert data["litellm_metadata"] == {}


@pytest.mark.parametrize("call_type", ["aresponses", "aanthropic_messages"])
def test_other_dialects_are_scrubbed_without_forcing_retry_kwargs(call_type: str) -> None:
    data = {"api_base": "http://attacker.invalid", "fallbacks": [{"model": "x"}]}

    _enforce_engine_routing(data, call_type)

    assert data == {}


def test_the_scrub_list_covers_litellms_former_blocklist() -> None:
    banned = _banned_request_params()

    assert {"api_base", "base_url", "fallbacks", "custom_llm_provider"} <= banned
    assert "azure_ad_token" in banned  # from LiteLLM's blocklist


# -- quota is handed back when nothing was generated ----------------------------------


def limited_catalog():
    return {
        "providers": {
            "alpha": provider(
                [model("solo", ["chat"], limits={"tpd": 100_000}, context_window=200_000)]
            )
        }
    }


def test_failed_requests_do_not_burn_the_token_budget() -> None:
    call = AsyncMock(side_effect=lambda **_: (_ for _ in ()).throw(
        provider_error(cls=llm.InternalServerError, status=500)))
    catalog = Catalog.from_data(limited_catalog())
    limits = catalog.models[0].limits
    with daemon(call, env=ENV, catalog=limited_catalog()) as (client, hook):
        post(client, {**CHAT, "max_tokens": 20_000})

        headroom = hook.quota.headroom(("alpha/solo", "default"), limits)

    # 20k tokens were only *reserved*; none were generated, so none may count.
    assert headroom > 0.99


# -- retry-after as provider errors deliver it ---------------------------------------


class _Err(Exception):
    def __init__(self, headers=None, response_headers=None):
        self.response_headers = headers
        self.response = type("R", (), {"headers": response_headers})() if response_headers is not None else None


def test_retry_after_is_read_from_the_provider_response_headers() -> None:
    assert _retry_after(_Err(headers={"retry-after": "7"})) == 7.0
    assert _retry_after(_Err(headers=None, response_headers={"retry-after": "9"})) == 9.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1e12", 86_400.0), ("inf", 86_400.0), ("nan", None), ("-5", None), ("abc", None)],
)
def test_retry_after_is_clamped_and_sanitised(raw: str, expected) -> None:
    assert _retry_after(_Err(headers={"retry-after": raw})) == expected


# -- escalation ------------------------------------------------------------------------


def test_a_truncated_answer_at_the_top_tier_is_not_replayed() -> None:
    call = AsyncMock(return_value=ok("cut off", finish_reason="length"))
    with daemon(call, env=ENV) as (client, _):
        response = post(client, {**CHAT, "model": "zhunt-reasoning"})

    assert response.status_code == 200
    assert call.await_count == 1  # nothing higher to promote to


def test_escalation_prefers_a_different_model_than_the_one_that_truncated() -> None:
    data = {
        "providers": {
            "alpha": provider(
                [
                    model("strong", ["chat", "coding"], intelligence=90, speed=90),
                    model("other", ["coding"], intelligence=40, speed=40),
                ]
            )
        }
    }
    call = AsyncMock(side_effect=[ok("cut off", finish_reason="length"), ok("full")])
    with daemon(call, env=ENV, catalog=data) as (client, _):
        response = post(client)

    assert response.status_code == 200
    first, second = (c.kwargs["model"] for c in call.await_args_list)
    assert first == "alpha/strong"
    assert second == "alpha/other"


# -- catalogs without every tier --------------------------------------------------------


def test_a_catalog_missing_tiers_still_gives_the_classifier_all_four() -> None:
    chat_only = Catalog.from_data({"providers": {"alpha": provider([model("m", ["chat"])])}})

    registry = registry_from_catalog(chat_only)

    for tier in Tier:
        assert registry.select_model(tier)  # no RegistryError -> no 500


# -- clocks ------------------------------------------------------------------------------


def test_reliability_does_not_grow_when_the_clock_steps_backwards() -> None:
    clock = FakeClock()
    reliability = Reliability(clock=clock)
    reliability.record("m", success=False)
    before = reliability.score("m")

    clock.advance(-30 * 86_400)

    assert reliability.score("m") == pytest.approx(before)


def test_a_failed_compare_run_does_not_burn_the_token_budget() -> None:
    tight = {
        "providers": {
            "alpha": provider(
                [model("solo", ["chat"], limits={"tpd": 1_000}, context_window=200_000)]
            )
        }
    }
    limits = Catalog.from_data(tight).models[0].limits
    call = AsyncMock(side_effect=lambda **_: (_ for _ in ()).throw(
        provider_error(cls=llm.InternalServerError, status=500)))
    with daemon(call, env=ENV, catalog=tight) as (client, hook):
        response = client.post(
            "/zhunt/api/compare",
            headers={"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}"},
            json={"messages": [{"role": "user", "content": "hi"}], "models": ["alpha/solo"]},
        )
        headroom = hook.quota.headroom(("alpha/solo", "default"), limits)

    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is False
    # compare reserves ~500 tokens of a 1000-token day; a failure must give them back.
    assert headroom > 0.99
