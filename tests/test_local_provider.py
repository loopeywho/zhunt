"""The local (Zhunt Plus / InferSSD) provider and the private-chat rule."""

import socket
from unittest.mock import AsyncMock, patch

import pytest

from tests.catalog_fixtures import catalog_data, model, provider
from tests.test_engine import TOOLS, CHAT, daemon, ok, post, provider_error
from zhunt import llm
from zhunt.catalog import Catalog, load_catalog
from zhunt.catalog_feed import ActiveCatalog
from zhunt.local_provider import (
    LocalConfig,
    LocalConfigError,
    config_from_env,
    is_private,
    with_local_provider,
)

LOCAL_ENV = {"FAKE_API_KEY": "k", "ZHUNT_LOCAL_PORT": "18080"}
PRIVATE = {"x-zhunt-private": "1"}
NOTHING_SENT = "nothing was sent to any cloud provider"


def post_private(client, body=CHAT, headers=PRIVATE):
    import os

    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}", **headers},
        json=body,
    )


@pytest.fixture
def local_up():
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=True)) as probe:
        yield probe


def is_local_call(call) -> bool:
    return call.kwargs.get("api_base") == "http://127.0.0.1:18080/v1"


# -- settings ----------------------------------------------------------------


def test_no_settings_means_no_local_provider() -> None:
    assert config_from_env({}) is None


def test_a_port_becomes_a_loopback_url_with_the_default_context() -> None:
    config = config_from_env({"ZHUNT_LOCAL_PORT": "7777"})
    assert config == LocalConfig(base_url="http://127.0.0.1:7777/v1", context_window=1024)


@pytest.mark.parametrize(
    "url", ["http://localhost:8080/v1/", "http://[::1]:8080/v1", "http://127.0.0.2:9/v1"]
)
def test_loopback_urls_are_accepted(url) -> None:
    assert config_from_env({"ZHUNT_LOCAL_URL": url}).base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "env",
    [
        {"ZHUNT_LOCAL_URL": "https://api.example.com/v1"},
        {"ZHUNT_LOCAL_URL": "http://192.168.1.5:8080/v1"},
        {"ZHUNT_LOCAL_URL": "http://localhost.example.com/v1"},
        {"ZHUNT_LOCAL_URL": "file:///tmp/x"},
        {"ZHUNT_LOCAL_PORT": "80a"},
        {"ZHUNT_LOCAL_PORT": "70000"},
        {"ZHUNT_LOCAL_PORT": "8080", "ZHUNT_LOCAL_CONTEXT": "0"},
    ],
)
def test_settings_that_could_leave_the_machine_or_are_malformed_are_rejected(env) -> None:
    with pytest.raises(LocalConfigError):
        config_from_env(env)


def test_context_model_and_tools_are_configurable() -> None:
    config = config_from_env(
        {
            "ZHUNT_LOCAL_PORT": "8080",
            "ZHUNT_LOCAL_CONTEXT": "4096",
            "ZHUNT_LOCAL_MODEL": "my-moe",
            "ZHUNT_LOCAL_TOOLS": "1",
        }
    )
    assert (config.context_window, config.model, config.supports_tools) == (4096, "my-moe", True)


def test_private_is_a_header_or_a_local_alias() -> None:
    assert is_private("zhunt-auto", {"X-Zhunt-Private": "true"})
    assert is_private("zhunt-local", None)
    assert is_private("zhunt-private", {})
    assert not is_private("zhunt-auto", {"x-zhunt-private": "0"})
    assert not is_private("zhunt-auto", None)


def test_a_catalog_file_cannot_mark_a_provider_local() -> None:
    data = catalog_data()
    data["providers"]["local"] = provider([model("m", ["chat"])], local=True)
    catalog = Catalog.from_data(data)
    assert catalog.providers["local"].local is False
    # ...and adding the real local provider replaces that entry
    replaced = with_local_provider(catalog, LocalConfig(base_url="http://127.0.0.1:1/v1"))
    assert replaced.providers["local"].local is True
    assert [m.id for m in replaced.models if m.platform == "local"] == ["local/local-model"]
    assert with_local_provider(catalog, None).providers.get("local") is None


# -- routing -----------------------------------------------------------------


def test_a_private_chat_goes_only_to_the_local_model(local_up) -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env=LOCAL_ENV) as (client, _):
        response = post_private(client)

    assert response.status_code == 200
    assert call.await_count == 1
    kwargs = call.await_args.kwargs
    assert kwargs["model"] == "openai/local-model"
    assert kwargs["api_base"] == "http://127.0.0.1:18080/v1"
    assert kwargs["api_key"] is None


def test_the_zhunt_local_alias_is_private_too(local_up) -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env=LOCAL_ENV) as (client, _):
        response = post(client, {**CHAT, "model": "zhunt-local"})

    assert response.status_code == 200
    assert is_local_call(call.await_args)


@pytest.mark.thompson
def test_ordinary_chats_prefer_cloud_models_even_with_sampled_reliability(local_up) -> None:
    call = AsyncMock(return_value=ok())
    # No published caps, so the cloud models never run out (the fixture's do).
    unlimited = {
        "providers": {
            "alpha": provider(
                [model("slowish", ["chat", "coding"], speed=5, intelligence=5)]
            )
        }
    }
    with daemon(call, env=LOCAL_ENV, catalog=unlimited) as (client, _):
        for _ in range(30):
            assert post(client).status_code == 200

    assert not any(is_local_call(c) for c in call.await_args_list)


def test_the_local_model_is_the_last_resort_after_every_cloud_model(local_up) -> None:
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if not is_local_call(type("C", (), {"kwargs": kwargs})):
            raise provider_error()
        return ok("from local")

    with daemon(AsyncMock(side_effect=fake), env=LOCAL_ENV) as (client, _):
        response = post(client)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from local"
    assert [is_local_call(type("C", (), {"kwargs": k})) for k in calls] == [False, False, True]


def test_a_private_chat_without_a_local_model_fails_and_calls_nothing() -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env={"FAKE_API_KEY": "k"}) as (client, _):
        response = post_private(client)

    assert response.status_code == 503
    assert NOTHING_SENT in response.text
    assert "zhunt local setup" in response.text
    call.assert_not_awaited()


def test_a_remote_local_url_is_refused_and_private_chats_say_why() -> None:
    call = AsyncMock(return_value=ok())
    env = {"FAKE_API_KEY": "k", "ZHUNT_LOCAL_URL": "https://api.example.com/v1"}
    with daemon(call, env=env) as (client, hook):
        response = post_private(client)
        assert any("Local model: not used" in line for line in hook.summary)

    assert response.status_code == 503
    assert "must point at this computer" in response.text
    call.assert_not_awaited()


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_private_chat_with_the_local_server_down_fails_clearly() -> None:
    call = AsyncMock(return_value=ok())
    env = {"FAKE_API_KEY": "k", "ZHUNT_LOCAL_PORT": str(_unused_port())}
    with daemon(call, env=env) as (client, _):
        response = post_private(client)

    assert response.status_code == 503
    assert "not answering" in response.text and NOTHING_SENT in response.text
    call.assert_not_awaited()


def test_a_local_failure_is_not_replayed_on_the_cloud_nor_benched(local_up) -> None:
    error = llm.APIConnectionError("APIConnectionError: connection refused")
    call = AsyncMock(side_effect=[error, ok("back")])
    with daemon(call, env=LOCAL_ENV) as (client, _):
        failed = post_private(client)
        recovered = post_private(client)

    assert failed.status_code >= 500
    assert recovered.status_code == 200
    assert call.await_count == 2
    assert all(is_local_call(c) for c in call.await_args_list)


def test_a_truncated_private_answer_is_not_rerun(local_up) -> None:
    call = AsyncMock(return_value=ok(finish_reason="length"))
    with daemon(call, env=LOCAL_ENV) as (client, _):
        response = post_private(client)

    assert response.status_code == 200
    assert call.await_count == 1


def test_a_private_chat_too_long_for_the_local_context_fails_clearly(local_up) -> None:
    call = AsyncMock(return_value=ok())
    long_chat = {**CHAT, "messages": [{"role": "user", "content": "word " * 2000}]}
    with daemon(call, env=LOCAL_ENV) as (client, _):
        response = post_private(client, long_chat)

    assert response.status_code == 503
    assert "too long for its context" in response.text and NOTHING_SENT in response.text
    call.assert_not_awaited()


def test_a_private_chat_with_tools_needs_a_tool_capable_local_model(local_up) -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env=LOCAL_ENV) as (client, _):
        refused = post_private(client, {**CHAT, "tools": TOOLS})
    assert refused.status_code == 503
    call.assert_not_awaited()

    with daemon(call, env={**LOCAL_ENV, "ZHUNT_LOCAL_TOOLS": "1"}) as (client, _):
        allowed = post_private(client, {**CHAT, "tools": TOOLS})
    assert allowed.status_code == 200
    assert is_local_call(call.await_args)


def test_a_new_catalog_from_the_feed_keeps_the_local_model(local_up) -> None:
    call = AsyncMock(return_value=ok())
    with daemon(call, env=LOCAL_ENV) as (client, hook):
        hook.apply_catalog(ActiveCatalog(load_catalog(), 1, "feed"))
        assert hook.catalog.providers["local"].local
        response = post_private(client)

    assert response.status_code == 200
    assert is_local_call(call.await_args)


@pytest.mark.parametrize(
    ("path", "function", "body"),
    [
        ("/v1/responses", "aresponses", {"model": "zhunt-auto", "input": "Hello"}),
        (
            "/v1/messages",
            "anthropic_messages",
            {"model": "zhunt-auto", "max_tokens": 50,
             "messages": [{"role": "user", "content": "Hello"}]},
        ),
    ],
)
def test_the_private_header_works_on_every_wire_format(local_up, path, function, body) -> None:
    import os

    upstream = AsyncMock(return_value=llm.ModelResponse({"id": "r", "output": []}))
    with daemon(AsyncMock(), env=LOCAL_ENV) as (client, _), patch(f"zhunt.llm.{function}", upstream):
        client.post(
            path,
            headers={"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}", **PRIVATE},
            json=body,
        )

    assert upstream.await_count == 1
    assert is_local_call(upstream.await_args)


def test_without_a_local_model_the_private_header_blocks_every_wire_format() -> None:
    import os

    upstream = AsyncMock()
    with daemon(AsyncMock(), env={"FAKE_API_KEY": "k"}) as (client, _), patch(
        "zhunt.llm.aresponses", upstream
    ):
        response = client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}", **PRIVATE},
            json={"model": "zhunt-auto", "input": "Hello"},
        )

    assert response.status_code == 503 and NOTHING_SENT in response.text
    upstream.assert_not_awaited()
