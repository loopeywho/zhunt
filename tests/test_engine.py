import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from tests.catalog_fixtures import catalog_data, model, provider
from zhunt.brain import Tier
from zhunt import llm
from zhunt.catalog import Catalog, Limits
from zhunt.engine import (
    classify_failure,
    keys_from_env,
    limits_from_headers,
    load_catalog,
    parse_limit_from_error,
    policy_from_env,
    registry_from_catalog,
)
from zhunt.quota import FailureKind as ProviderFailure
from zhunt.server import create_proxy_app

CHAT = {"model": "zhunt-auto", "messages": [{"role": "user", "content": "Hello"}]}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def ok(content: str = "hi", finish_reason: str = "stop") -> llm.ModelResponse:
    return llm.ModelResponse(dict(
        id="r",
        model="x",
        choices=[
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
    ))


def provider_error(
    cls=llm.RateLimitError,
    status: int = 429,
    retry_after: int | None = None,
) -> Exception:
    headers = {"retry-after": str(retry_after)} if retry_after else {}
    return cls(message="boom", status_code=status, response_headers=httpx.Headers(headers))


@contextmanager
def daemon(provider_call: AsyncMock, *, env: dict[str, str], catalog=None):
    """Run the free-tier daemon against a fake catalog with the provider call patched."""

    with TemporaryDirectory() as directory:
        catalog_path = Path(directory) / "catalog.yaml"
        catalog_path.write_text(yaml.safe_dump(catalog or catalog_data()))
        clean = {
            "ZHUNT_ALLOW_PAID": "",
            "ZHUNT_ALLOW_TRAINING": "",
            "FAKE_API_KEY": "",
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "CEREBRAS_API_KEY": "",
        }
        with patch.dict(os.environ, {**clean, **env}), patch(
            "zhunt.llm.acompletion", provider_call
        ):
            app = create_proxy_app(
                catalog_path=catalog_path if catalog is not False else None,
                env_path=Path(directory) / "zhunt.env",
            )
            with TestClient(app) as client:
                yield client, app.hook


def post(client, body=CHAT):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}"},
        json=body,
    )


# -- catalog + config ------------------------------------------------------


def test_packaged_catalog_covers_every_tier_with_tool_capable_models() -> None:
    catalog = load_catalog()

    for tier in Tier:
        models = catalog.models_for_tier(tier)
        assert models, f"no model for tier {tier.value}"
        assert any(m.supports_tools for m in models)
    assert {p.platform for p in catalog.providers.values()} == {
        "groq",
        "google",
        "cerebras",
        "kilo",
        "zai",
        "mistral",
        "sambanova",
        "ovh",
        "anthropic",
        "openai",
        "google-paid",
    }
    # premium models come only from the companies themselves, never a reseller
    paid = [m for m in catalog.models if not m.free]
    assert {m.platform for m in paid} == {"anthropic", "openai", "google-paid"}
    assert not any(m.may_train_on_prompts for m in paid)
    # paid Gemini uses its own key, never the free-tier one
    assert catalog.providers["google-paid"].key_env != catalog.providers["google"].key_env
    for tier in Tier:
        assert any(m.free for m in catalog.models_for_tier(tier)), f"no free model for {tier.value}"
    assert registry_from_catalog(catalog).model_ids()


def test_only_platforms_with_a_key_are_routable() -> None:
    catalog = Catalog.from_data(catalog_data())

    assert keys_from_env(catalog, {"FAKE_API_KEY": "k"}) == {
        p: ["default"] for p in catalog.providers
    }
    assert keys_from_env(catalog, {}) == {}


def test_policy_is_off_unless_explicitly_enabled() -> None:
    assert not policy_from_env({}).allow_paid
    assert not policy_from_env({"ZHUNT_ALLOW_TRAINING": "0"}).allow_training
    assert policy_from_env({"ZHUNT_ALLOW_TRAINING": "1"}).allow_training
    assert policy_from_env({"ZHUNT_ALLOW_PAID": "true"}).allow_paid


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ProviderFailure.RATE_LIMIT),
        (402, ProviderFailure.CREDIT),
        (401, ProviderFailure.AUTH),
        (403, ProviderFailure.FORBIDDEN),
        (404, ProviderFailure.NOT_FOUND),
        (410, ProviderFailure.NOT_FOUND),
        (500, ProviderFailure.SERVER),
        (503, ProviderFailure.SERVER),
        (408, ProviderFailure.SERVER),
        (400, None),
        (422, None),
    ],
)
def test_failure_kinds_by_status(status: int, kind) -> None:
    error = RuntimeError("x")
    error.status_code = status

    assert classify_failure(error)[0] == kind


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("You have used up your daily free allocation of 10,000 neurons",
         ProviderFailure.DAILY_QUOTA),
        ("Rate limit exceeded: free-models-per-day. Quota resets at midnight",
         ProviderFailure.DAILY_QUOTA),
        ("Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel-FreeTier, "
         "limit: 20", ProviderFailure.DAILY_QUOTA),
        # Groq's per-minute 429 ends in sales copy that says "today".
        ("Rate limit reached on tokens per minute (TPM): Limit 6000, Used 5000, "
         "Requested 2000. Need more tokens? Upgrade to Dev Tier today at "
         "https://console.groq.com/settings/billing", ProviderFailure.RATE_LIMIT),
        ("Too many requests", ProviderFailure.RATE_LIMIT),
    ],
)
def test_a_429_saying_the_daily_quota_is_spent_is_told_apart(message, kind) -> None:
    error = RuntimeError(message)
    error.status_code = 429

    assert classify_failure(error)[0] == kind


def test_an_error_without_a_status_counts_as_a_server_failure() -> None:
    assert classify_failure(RuntimeError("down"))[0] == ProviderFailure.SERVER


def test_retry_after_header_is_read() -> None:
    assert classify_failure(provider_error(retry_after=7)) == (
        ProviderFailure.RATE_LIMIT,
        7.0,
    )


# -- daemon behaviour ------------------------------------------------------


def test_request_goes_to_a_free_model_with_that_providers_key() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={"FAKE_API_KEY": "key-123"}) as (client, _):
        response = post(client)

    assert response.status_code == 200
    kwargs = call.await_args.kwargs
    assert kwargs["model"] in {"alpha/chat-fast", "alpha/coder"}
    assert kwargs["api_key"] == "key-123"


def test_rate_limited_model_fails_over_and_is_benched() -> None:
    call = AsyncMock(side_effect=[provider_error(retry_after=7), ok("recovered")])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, hook):
        response = post(client)
        first = call.await_args_list[0].kwargs["model"]
        slot = (first, "default")

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "recovered"
        assert call.await_count == 2
        assert call.await_args_list[1].kwargs["model"] != first
        # A per-minute 429: the 7 s retry-after is only a floor under 90 s.
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(90, abs=1)


def test_several_failures_in_a_row_still_find_a_working_model() -> None:
    call = AsyncMock(
        side_effect=[provider_error(), provider_error(status=500, cls=llm.InternalServerError), ok()]
    )
    env = {"FAKE_API_KEY": "k", "ZHUNT_ALLOW_TRAINING": "1"}
    with daemon(call, env=env) as (client, _):
        response = post(client)

    assert response.status_code == 200
    assert call.await_count == 3
    assert len({c.kwargs["model"] for c in call.await_args_list}) == 3


def test_every_model_failing_ends_in_503_not_a_loop() -> None:
    call = AsyncMock(side_effect=lambda **_: (_ for _ in ()).throw(provider_error()))
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, _):
        response = post(client)

    assert response.status_code == 503
    assert call.await_count == 2  # chat-fast and coder; the others are gated off


def test_client_errors_do_not_bench_the_model_or_retry() -> None:
    error = provider_error(cls=llm.BadRequestError, status=400)
    call = AsyncMock(side_effect=[error])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, hook):
        response = post(client)
        slot = (call.await_args.kwargs["model"], "default")

        assert response.status_code == 400
        assert call.await_count == 1
        assert hook.quota.cooldown_remaining(slot) == 0


def test_no_provider_key_means_no_call_and_a_503() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={}) as (client, _):
        response = post(client)

    assert response.status_code == 503
    call.assert_not_awaited()


def packaged_with_keys_only() -> dict:
    """The packaged catalog without the providers that answer with no key at all."""

    data = yaml.safe_load((Path(__file__).parents[1] / "zhunt" / "catalog.yaml").read_text())
    data["providers"] = {k: v for k, v in data["providers"].items() if not v.get("keyless")}
    return data


def test_prompt_training_provider_needs_opt_in_and_uses_the_gemini_route() -> None:
    call = AsyncMock(return_value=ok())
    only_google = {"GEMINI_API_KEY": "g-key"}
    catalog = packaged_with_keys_only()

    with daemon(call, env=only_google, catalog=catalog) as (client, _):
        blocked = post(client)
    with daemon(call, env={**only_google, "ZHUNT_ALLOW_TRAINING": "1"}, catalog=catalog) as (
        client,
        _,
    ):
        allowed = post(client)

    assert blocked.status_code == 503
    assert allowed.status_code == 200
    kwargs = call.await_args.kwargs
    assert kwargs["model"].startswith("gemini/gemini-3.")
    assert kwargs["api_key"] == "g-key"


def test_with_no_keys_and_no_opt_in_a_keyless_no_training_provider_answers() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={}, catalog=False) as (client, _):
        response = post(client)

    assert response.status_code == 200
    assert call.await_args.kwargs["model"].startswith("openai/")
    assert call.await_args.kwargs["api_base"] == "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"


def test_requests_with_tools_only_go_to_tool_capable_models() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, _):
        response = post(client, {**CHAT, "tools": TOOLS})

    assert response.status_code == 200
    assert call.await_args.kwargs["model"] == "alpha/coder"


def test_truncated_answer_is_retried_on_a_higher_tier() -> None:
    call = AsyncMock(side_effect=[ok("cut off", finish_reason="length"), ok("full")])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, _):
        response = post(client)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "full"
    assert call.await_count == 2


def test_tool_requests_skip_a_stronger_model_that_lacks_tool_support() -> None:
    data = catalog_data()
    data["providers"]["alpha"]["models"].append(
        {
            "id": "notools",
            "tiers": ["coding"],
            "intelligence": 100,
            "speed": 100,
            "context_window": 32_000,
            "supports_tools": False,
            "may_train_on_prompts": False,
        }
    )
    call = AsyncMock(return_value=ok())
    with daemon(call, env={"FAKE_API_KEY": "k"}, catalog=data) as (client, _):
        with_tools = post(client, {**CHAT, "tools": TOOLS})
        tools_model = call.await_args.kwargs["model"]
        without_tools = post(client, {**CHAT, "model": "zhunt-coding"})
        plain_model = call.await_args.kwargs["model"]

    assert with_tools.status_code == without_tools.status_code == 200
    assert tools_model == "alpha/coder"
    assert plain_model == "alpha/notools"  # the filter, not scoring, excluded it


def test_a_success_resets_the_failure_streak_so_cooldowns_start_over() -> None:
    from tests.catalog_fixtures import FakeClock, model, provider

    solo = {"providers": {"alpha": provider([model("solo", ["chat"])])}}
    clock = FakeClock()
    call = AsyncMock(side_effect=[provider_error(), ok(), provider_error()])
    with daemon(call, env={"FAKE_API_KEY": "k"}, catalog=solo) as (client, hook):
        hook.quota._clock = clock
        slot = ("alpha/solo", "default")

        assert post(client).status_code == 503  # 429, nothing else to fail over to
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(90)
        clock.advance(91)
        assert post(client).status_code == 200  # recovers
        assert post(client).status_code == 503  # fails again
        # Without the reset a second 429 within the hour would escalate (120s).
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(90)


@pytest.mark.parametrize(
    ("cls", "status", "whole_provider", "seconds"),
    [
        (llm.AuthenticationError, 401, True, 300),
        # Cerebras and Groq report a 401 this way
        (llm.BadRequestError, 401, True, 300),
        (llm.BadRequestError, 402, True, 86_400),
        # A 403 is this model off the key's tier, a 404 this model gone: neither
        # condemns the provider's other models.
        (llm.PermissionDeniedError, 403, False, 86_400),
        (llm.NotFoundError, 404, False, 86_400),
    ],
)
def test_a_rejected_key_benches_every_model_on_that_provider(
    cls, status: int, whole_provider: bool, seconds: int
) -> None:
    call = AsyncMock(side_effect=[provider_error(cls=cls, status=status), ok()])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, hook):
        response = post(client)
        failed = call.await_args_list[0].kwargs["model"]
        other = next(m for m in ("alpha/chat-fast", "alpha/coder") if m != failed)

        benched = hook.quota.cooldown_remaining((other, "default")) > 0
        assert hook.quota.cooldown_remaining((failed, "default")) == pytest.approx(
            seconds, abs=1
        )

    assert benched is whole_provider
    if whole_provider:
        assert response.status_code == 503  # nothing else left to try
        assert call.await_count == 1
    else:
        assert response.status_code == 200
        assert call.await_count == 2


@pytest.mark.parametrize(
    ("message", "learned"),
    [
        ("Rate limit reached for model `llama` on tokens per minute (TPM): Limit 6000, "
         "Used 5000, Requested 2000.", ("tpm", 6_000)),
        ("Request too large on tokens per day (TPD): Limit 500,000, Requested 600,000",
         ("tpd", 500_000)),
        ("Rate limit reached on requests per day (RPD): Limit 1000, Used 1000",
         ("rpd", 1_000)),
        ("Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel-FreeTier, "
         "limit: 20", ("rpd", 20)),
        ("Too many requests, limit 30", None),  # no axis: never guess
        ("Rate limit exceeded on tokens per minute", None),  # no number
    ],
)
def test_limits_stated_in_error_bodies_are_parsed(message, learned) -> None:
    assert parse_limit_from_error(message) == learned


def test_limits_are_read_from_provider_rate_limit_headers() -> None:
    groq = {
        "x-ratelimit-limit-requests": "14400",
        "x-ratelimit-limit-tokens": "6000",
    }
    cerebras = httpx.Headers(
        {"x-ratelimit-limit-requests-day": "14400", "x-ratelimit-limit-tokens-minute": "60000"}
    )

    assert limits_from_headers("groq", groq) == [("rpd", 14_400), ("tpm", 6_000)]
    assert limits_from_headers("cerebras", cerebras) == [("rpd", 14_400), ("tpm", 60_000)]
    assert limits_from_headers("alpha", {"x-ratelimit-limit-requests": "30"}) == [("rpm", 30)]
    assert limits_from_headers("alpha", {"x-ratelimit-limit-requests": "soon"}) == []
    assert limits_from_headers("alpha", None) == []


def test_a_429_body_teaches_the_model_its_real_limit() -> None:
    error = provider_error()
    error.message = "on tokens per minute (TPM): Limit 6000, Used 5000, Requested 2000"
    call = AsyncMock(side_effect=[error, ok()])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, hook):
        post(client)
        failed = call.await_args_list[0].kwargs["model"]
        limits = hook.quota.effective_limits(failed, Limits())

    assert limits.tpm == 6_000


def test_success_headers_lower_a_known_limit_but_never_raise_one() -> None:
    response = ok()
    response.headers = httpx.Headers(
        {"x-ratelimit-limit-requests": "4", "x-ratelimit-limit-tokens": "1"}
    )
    raised = ok()
    raised.headers = httpx.Headers({"x-ratelimit-limit-requests": "50"})
    solo = {"providers": {"alpha": provider([model("solo", ["chat"], limits={"rpm": 5})])}}
    call = AsyncMock(side_effect=[raised, response])
    with daemon(call, env={"FAKE_API_KEY": "k"}, catalog=solo) as (client, hook):
        post(client)
        after_raise = hook.quota.effective_limits("alpha/solo", Limits(rpm=5))
        post(client)
        after_lower = hook.quota.effective_limits("alpha/solo", Limits(rpm=5))

    assert after_raise == Limits(rpm=5)
    assert after_lower == Limits(rpm=4, tpm=1)


def test_failures_and_successes_move_the_live_penalty() -> None:
    call = AsyncMock(
        side_effect=[
            provider_error(),
            provider_error(status=500, cls=llm.InternalServerError),
            ok(),
        ]
    )
    env = {"FAKE_API_KEY": "k", "ZHUNT_ALLOW_TRAINING": "1"}
    with daemon(call, env=env) as (client, hook):
        post(client)
        first, second, third = (c.kwargs["model"] for c in call.await_args_list)
        penalties = hook.selector.penalties

        assert penalties.penalty(first) == 3  # a 429
        assert penalties.penalty(second) == 1  # a 5xx
        assert penalties.penalty(third) == 0


@pytest.mark.thompson
def test_production_selection_samples_reliability_instead_of_using_the_mean() -> None:
    with daemon(AsyncMock(return_value=ok()), env={"FAKE_API_KEY": "k"}) as (_, hook):
        draws = {hook.reliability.score("alpha/coder") for _ in range(20)}
        mean = hook.reliability.mean("alpha/coder")

    assert len(draws) > 1  # Thompson samples vary; the mean would not
    assert mean == 0.5


def test_a_spent_google_daily_quota_benches_until_pacific_midnight() -> None:
    import time

    from zhunt.quota import seconds_until_midnight

    error = provider_error(retry_after=20)
    error.message = "Quota exceeded for metric: GenerateRequestsPerDayPerProjectPerModel"
    call = AsyncMock(side_effect=[error, ok()])
    env = {"GEMINI_API_KEY": "g", "ZHUNT_ALLOW_TRAINING": "1"}
    with daemon(call, env=env, catalog=False) as (client, hook):
        post(client)
        failed = call.await_args_list[0].kwargs["model"].replace("gemini/", "google/")
        remaining = hook.quota.cooldown_remaining((failed, "default"))

    expected = seconds_until_midnight(time.time(), "America/Los_Angeles")
    assert remaining == pytest.approx(expected, abs=5)


# -- premium fallback (Pro) ------------------------------------------------

PRO_PLAN = __import__("zhunt.licence", fromlist=["Plan"]).Plan(
    name="pro", features=frozenset({"premium_fallback"}), expires_at=2e9
)


def premium_catalog() -> dict:
    """One free model and one premium model on its own key."""

    return {
        "providers": {
            "alpha": provider([model("free-chat", ["chat"], intelligence=40, speed=90)]),
            "router": provider(
                [model("premium", ["chat"], intelligence=95, speed=60, **{"in": 0.001, "out": 0.002})],
                key_env="ROUTER_API_KEY",
            ),
        }
    }


def usage_reply() -> llm.ModelResponse:
    response = ok()
    response["usage"] = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return response


def test_premium_takes_over_when_free_models_fail_and_its_cost_is_recorded() -> None:
    def answer(**kwargs):
        if kwargs["model"].endswith("free-chat"):
            raise provider_error()
        return usage_reply()

    call = AsyncMock(side_effect=answer)
    env = {"FAKE_API_KEY": "free-key", "ROUTER_API_KEY": "paid-key", "ZHUNT_ALLOW_PAID": "1"}
    with patch("zhunt.licence.current_plan", return_value=PRO_PLAN), daemon(
        call, env=env, catalog=premium_catalog()
    ) as (client, hook):
        response = post(client)
        spent = hook.spend.spent()

    assert response.status_code == 200
    paid_call = call.await_args.kwargs
    assert paid_call["model"].endswith("premium") and paid_call["api_key"] == "paid-key"
    assert spent == pytest.approx(100 * 0.001 + 50 * 0.002)


def test_without_pro_a_failing_free_model_is_a_503_not_a_paid_call() -> None:
    call = AsyncMock(side_effect=provider_error())
    env = {"FAKE_API_KEY": "free-key", "ROUTER_API_KEY": "paid-key", "ZHUNT_ALLOW_PAID": "1"}
    with daemon(call, env=env, catalog=premium_catalog()) as (client, hook):
        response = post(client)
        spent = hook.spend.spent()

    assert response.status_code == 503
    assert all(not c.kwargs["model"].endswith("premium") for c in call.await_args_list)
    assert spent == 0


def test_premium_stops_at_the_monthly_cap() -> None:
    call = AsyncMock(side_effect=provider_error())
    env = {"FAKE_API_KEY": "k", "ROUTER_API_KEY": "paid-key", "ZHUNT_ALLOW_PAID": "1", "ZHUNT_MONTHLY_CAP": "0.5"}
    with patch("zhunt.licence.current_plan", return_value=PRO_PLAN), daemon(
        call, env=env, catalog=premium_catalog()
    ) as (client, hook):
        hook.spend.add("router/premium", 0.6)
        response = post(client)

    assert response.status_code == 503
    assert all(not c.kwargs["model"].endswith("premium") for c in call.await_args_list)


def test_client_supplied_extra_body_never_reaches_the_provider() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, _):
        post(client, {**CHAT, "extra_body": {"provider": {"data_collection": "allow"}}})

    assert "extra_body" not in call.await_args.kwargs



def test_a_keyless_provider_is_routable_with_no_key_and_gets_its_own_endpoint() -> None:
    keyless_catalog = {
        "providers": {
            "anon": provider(
                [model("free-auto", ["chat"])],
                key_env="ANON_API_KEY", keyless=True, litellm_provider="openai",
                base_url="https://anon.example/v1",
            )
        }
    }
    call = AsyncMock(return_value=ok())
    with daemon(call, env={}, catalog=keyless_catalog) as (client, _):
        response = post(client)

    assert response.status_code == 200
    kwargs = call.await_args.kwargs
    assert kwargs["model"] == "openai/free-auto"
    assert kwargs["api_base"] == "https://anon.example/v1"
    assert kwargs["api_key"] is None  # no Authorization header: OVHcloud rejects a made-up key


def test_named_provider_routes_never_get_an_api_base_override() -> None:
    catalog = load_catalog()
    assert catalog.providers["groq"].api_base is None
    assert catalog.providers["kilo"].api_base == "https://api.kilo.ai/api/gateway"
