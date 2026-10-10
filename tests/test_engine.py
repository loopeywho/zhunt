import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
import litellm
import pytest
import yaml
from fastapi.testclient import TestClient
from litellm.proxy import proxy_server

from tests.catalog_fixtures import catalog_data
from zhunt.brain import Tier
from zhunt.catalog import Catalog
from zhunt.engine import (
    classify_failure,
    keys_from_env,
    load_catalog,
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


def ok(content: str = "hi", finish_reason: str = "stop") -> litellm.ModelResponse:
    return litellm.ModelResponse(
        id="r",
        model="x",
        choices=[
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
    )


def provider_error(
    cls=litellm.RateLimitError,
    status: int = 429,
    retry_after: int | None = None,
) -> Exception:
    headers = {"retry-after": str(retry_after)} if retry_after else {}
    response = httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("POST", "https://fake.invalid/v1"),
    )
    return cls(message="boom", llm_provider="alpha", model="alpha/m", response=response)


@contextmanager
def daemon(provider_call: AsyncMock, *, env: dict[str, str], catalog=None):
    """Run the free-tier daemon against a fake catalog with litellm patched."""

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
            "litellm.acompletion", provider_call
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
        headers={"Authorization": f"Bearer {proxy_server.master_key}"},
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
    }
    assert all(m.free for m in catalog.models)
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
        (403, ProviderFailure.AUTH),
        (404, ProviderFailure.AUTH),
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
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(7, abs=1)


def test_several_failures_in_a_row_still_find_a_working_model() -> None:
    call = AsyncMock(
        side_effect=[provider_error(), provider_error(status=500, cls=litellm.InternalServerError), ok()]
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
    error = provider_error(cls=litellm.BadRequestError, status=400)
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


def test_prompt_training_provider_needs_opt_in_and_uses_litellms_gemini_name() -> None:
    call = AsyncMock(return_value=ok())
    only_google = {"GEMINI_API_KEY": "g-key"}

    with daemon(call, env=only_google, catalog=False) as (client, _):
        blocked = post(client)
    with daemon(call, env={**only_google, "ZHUNT_ALLOW_TRAINING": "1"}, catalog=False) as (
        client,
        _,
    ):
        allowed = post(client)

    assert blocked.status_code == 503
    assert allowed.status_code == 200
    kwargs = call.await_args.kwargs
    assert kwargs["model"].startswith("gemini/gemini-3.")
    assert kwargs["api_key"] == "g-key"


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
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(120)
        clock.advance(121)
        assert post(client).status_code == 200  # recovers
        assert post(client).status_code == 503  # fails again
        # Without the reset this would be the second step (600s), not 120s.
        assert hook.quota.cooldown_remaining(slot) == pytest.approx(120)


@pytest.mark.parametrize(
    ("cls", "status", "whole_provider"),
    [
        (litellm.AuthenticationError, 401, True),
        (litellm.BadRequestError, 401, True),  # Cerebras and Groq report a 401 this way
        (litellm.PermissionDeniedError, 403, False),
        (litellm.NotFoundError, 404, False),
    ],
)
def test_a_rejected_key_benches_every_model_on_that_provider(
    cls, status: int, whole_provider: bool
) -> None:
    call = AsyncMock(side_effect=[provider_error(cls=cls, status=status), ok()])
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, hook):
        response = post(client)
        failed = call.await_args_list[0].kwargs["model"]
        other = next(m for m in ("alpha/chat-fast", "alpha/coder") if m != failed)

        benched = hook.quota.cooldown_remaining((other, "default")) > 0
        assert hook.quota.cooldown_remaining((failed, "default")) > 0

    assert benched is whole_provider
    if whole_provider:
        assert response.status_code == 503  # nothing else left to try
        assert call.await_count == 1
    else:
        assert response.status_code == 200
        assert call.await_count == 2
