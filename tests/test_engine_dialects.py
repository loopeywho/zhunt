"""Regression coverage for the three wire dialects on the free-tier engine.

Ported from the v1 server tests. Every scenario runs through the real app
(``create_proxy_app()``) or the real ``EngineProxyHook`` with the
provider call patched, and asserts on what the provider was asked (model,
api_key), on HTTP status/body, and on call counts.

The fake catalog has one model per tier, so a tier maps to exactly one model:

    chat -> alpha/chat-m          (key FAKE_API_KEY)
    coding -> alpha/code-m        (key FAKE_API_KEY)
    long-context -> beta/long-m   (key BETA_API_KEY)
    reasoning -> beta/reason-m    (key BETA_API_KEY)
"""

from __future__ import annotations

import json
import os
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.catalog_fixtures import model, provider
from tests.representative_prompts import AGENT_SYSTEM_PROMPT
from zhunt import llm
from zhunt.brain import Tier
from zhunt.engine import MAX_REPLAYS, build_engine_hook
from zhunt.quota import FailureKind as ProviderFailure
from zhunt.router import FailureKind, RoutingRequest
from zhunt.server import create_proxy_app

ALPHA_KEY = "alpha-key"
BETA_KEY = "beta-key"
KEYS = {"FAKE_API_KEY": ALPHA_KEY, "BETA_API_KEY": BETA_KEY}
_CLEAN_ENV = {
    "ZHUNT_ALLOW_PAID": "",
    "ZHUNT_ALLOW_TRAINING": "",
    "FAKE_API_KEY": "",
    "BETA_API_KEY": "",
}

CHAT = "alpha/chat-m"
CODING = "alpha/code-m"
LONG = "beta/long-m"
REASONING = "beta/reason-m"


def tier_catalog() -> dict[str, Any]:
    """One free model per tier, spread over two providers with different keys."""

    return {
        "providers": {
            "alpha": provider(
                [
                    model("chat-m", ["chat"], intelligence=40, speed=80),
                    model("code-m", ["coding"], supports_tools=True, intelligence=60),
                ]
            ),
            "beta": provider(
                [
                    model(
                        "long-m", ["long-context"], supports_tools=True,
                        context_window=1_000_000, intelligence=70,
                    ),
                    model("reason-m", ["reasoning"], supports_tools=True, intelligence=90),
                ],
                key_env="BETA_API_KEY",
            ),
        }
    }


def add_models(data: dict[str, Any], platform: str, *models: dict[str, Any]) -> dict[str, Any]:
    data["providers"][platform]["models"].extend(models)
    return data


# -- provider responses ---------------------------------------------------


def chat_reply(
    content: str = "ok", finish_reason: str = "stop", usage: dict | None = None
) -> llm.ModelResponse:
    extra = {"usage": usage} if usage else {}
    return llm.ModelResponse(dict(
        id="chat",
        model="x",
        choices=[
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        **extra,
    ))


def responses_reply(status: str = "completed") -> llm.ModelResponse:
    return llm.ModelResponse(dict(
        id="resp",
        created_at=1,
        model="x",
        object="response",
        output=[],
        status=status,
    ))


def messages_reply(
    text: str = "ok", stop_reason: str = "end_turn"
) -> llm.ModelResponse:
    return llm.ModelResponse(dict(
        id="msg",
        type="message",
        role="assistant",
        model="x",
        content=[{"type": "text", "text": text}],
        stop_reason=stop_reason,
        usage={"input_tokens": 1, "output_tokens": 1},
    ))


def stream_of(finish_reasons_and_text: list[tuple[str | None, str]], model_name: str = "x"):
    async def provider_stream():
        for finish_reason, text in finish_reasons_and_text:
            yield llm.ModelResponse(dict(
                id="stream",
                model=model_name,
                choices=[
                    {"index": 0, "finish_reason": finish_reason, "delta": {"content": text}}
                ],
            ))

    return provider_stream()


def provider_error(
    cls=llm.InternalServerError, status: int = 500
) -> Exception:
    return cls(message="boom", status_code=status)


# -- harness --------------------------------------------------------------


@contextmanager
def daemon(
    *,
    chat: AsyncMock | None = None,
    responses: AsyncMock | None = None,
    messages: AsyncMock | None = None,
    env: dict[str, str] | None = None,
    catalog: dict[str, Any] | None = None,
    telemetry_path: Path | None = None,
):
    """The real free-tier app against a fake catalog, one patched call per dialect."""

    with TemporaryDirectory() as directory, ExitStack() as stack:
        catalog_path = Path(directory) / "catalog.yaml"
        catalog_path.write_text(yaml.safe_dump(catalog or tier_catalog()))
        stack.enter_context(patch.dict(os.environ, {**_CLEAN_ENV, **(KEYS if env is None else env)}))
        for target, mock in (
            ("zhunt.llm.acompletion", chat),
            ("zhunt.llm.aresponses", responses),
            ("zhunt.llm.anthropic_messages", messages),
        ):
            if mock is not None:
                stack.enter_context(patch(target, mock))
        app = create_proxy_app(
            catalog_path=catalog_path,
            env_path=Path(directory) / "zhunt.env",
            telemetry_path=telemetry_path,
        )
        with TestClient(app) as client:
            yield client, app.hook


def auth(session: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {os.environ['ZHUNT_MASTER_KEY']}"}
    if session is not None:
        headers["x-session-id"] = session
    return headers


def post_chat(client, body: dict, session: str | None = None):
    return client.post("/v1/chat/completions", headers=auth(session), json=body)


def post_responses(client, body: dict, session: str | None = None):
    return client.post("/v1/responses", headers=auth(session), json=body)


def post_messages(client, body: dict, session: str | None = None):
    return client.post("/v1/messages", headers=auth(session), json=body)


def called_models(call: AsyncMock) -> list[str]:
    return [c.kwargs["model"] for c in call.await_args_list]


def chat_body(text: str = "Hello", model_name: str = "zhunt-auto", **extra: Any) -> dict:
    return {"model": model_name, "messages": [{"role": "user", "content": text}], **extra}


def first_content(response) -> str:
    return response.json()["choices"][0]["message"]["content"]


# -- recipe payloads (real app request shapes) -----------------------------

REALISTIC_CLAUDE_SYSTEM = [
    {
        "type": "text",
        "text": "You are Claude Code, Anthropic's official CLI for Claude.",
        "cache_control": {"type": "ephemeral"},
    },
    {
        "type": "text",
        "text": "Follow repository instructions and use tools for workspace tasks.",
    },
]

REALISTIC_SYSTEM = [
    {"type": "text", "text": "You are an assistant integrated into a development workspace."},
    {"type": "text", "text": "Follow repository instructions and use tools when needed."},
]

REALISTIC_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]

REALISTIC_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
]

TURNS = ("Thanks.", "Continue.", "What changed?", "Looks good.")


def claude_payloads() -> list[tuple[str, dict]]:
    return [
        (
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "system": REALISTIC_CLAUDE_SYSTEM,
                "tools": REALISTIC_TOOLS,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": text}],
            },
        )
        for text in TURNS
    ]


def codex_payloads() -> list[tuple[str, dict]]:
    return [
        (
            "/v1/responses",
            {
                "model": "zhunt-auto",
                "instructions": (
                    "You are Codex, an engineering agent. Follow repository "
                    "instructions and use tools when needed."
                ),
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read a workspace file.",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    }
                ],
                "max_output_tokens": 8192,
                "input": (
                    [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        }
                    ]
                    + (
                        [
                            {
                                "type": "function_call_output",
                                "call_id": "call_read_file",
                                "output": "file contents",
                            }
                        ]
                        if index > 0
                        else []
                    )
                ),
            },
        )
        for index, text in enumerate(TURNS)
    ]


def chat_recipe_payloads(recipe: str) -> list[tuple[str, dict]]:
    def payload(index: int, text: str) -> dict:
        if recipe == "hermes":
            return {
                "model": "zhunt-auto",
                "messages": [
                    {"role": "system", "content": REALISTIC_SYSTEM},
                    {"role": "user", "content": text},
                ],
                "tools": REALISTIC_CHAT_TOOLS if index == 0 else [],
                "max_tokens": 8192,
            }
        if recipe == "cursor":
            return {
                "model": "zhunt-auto",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are Cursor's coding assistant. Use workspace tools.",
                    },
                    {"role": "user", "content": text},
                ],
                "tools": REALISTIC_CHAT_TOOLS if index == 0 else [],
                "max_completion_tokens": 8192,
            }
        messages: list[dict] = [
            {
                "role": "developer",
                "content": [{"type": "text", "text": "You are VS Code's coding assistant."}],
            },
            {"role": "user", "content": text},
        ]
        if index == 1:
            messages.insert(
                1,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read_file",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"README.md"}',
                            },
                        }
                    ],
                },
            )
        return {
            "model": "zhunt-auto",
            "messages": messages,
            "tools": REALISTIC_CHAT_TOOLS if index == 0 else [],
            "max_tokens": 8192,
        }

    return [("/v1/chat/completions", payload(i, t)) for i, t in enumerate(TURNS)]


def assert_session_stays_on_coding(
    dialect: str, payloads: list[tuple[str, dict]], mocks: dict[str, AsyncMock]
) -> None:
    session = f"realistic-{dialect}"
    with daemon(**mocks) as (client, hook):
        responses = [
            client.post(path, headers=auth(session), json=payload)
            for path, payload in payloads
        ]
        decisions = hook.decisions(limit=50)

    (call,) = mocks.values()
    assert [r.status_code for r in responses] == [200] * len(payloads)
    assert call.await_count == len(payloads)
    assert called_models(call) == [CODING] * len(payloads)
    assert {c.kwargs["api_key"] for c in call.await_args_list} == {ALPHA_KEY}
    assert [d["tier"] for d in decisions] == ["coding"] * len(payloads)
    assert all(d["success"] and not d["degraded"] for d in decisions)


def test_claude_recipe_payload_stays_on_coding_tier() -> None:
    assert_session_stays_on_coding(
        "messages", claude_payloads(), {"messages": AsyncMock(return_value=messages_reply())}
    )


def test_codex_recipe_payload_stays_on_coding_tier() -> None:
    assert_session_stays_on_coding(
        "responses", codex_payloads(), {"responses": AsyncMock(return_value=responses_reply())}
    )


@pytest.mark.parametrize("recipe", ["hermes", "cursor", "vscode"])
def test_chat_recipe_payload_stays_on_coding_tier(recipe: str) -> None:
    assert_session_stays_on_coding(
        recipe, chat_recipe_payloads(recipe), {"chat": AsyncMock(return_value=chat_reply())}
    )


# -- pre-call hook, driven directly ---------------------------------------


def proxy_data(
    body: dict, *, call_id: str, headers: dict[str, str] | None = None
) -> dict:
    return {
        "model": body["model"],
        "litellm_call_id": call_id,
        "proxy_server_request": {"body": body, "headers": headers or {}},
    }


class EngineHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        path = Path(self._directory.name) / "catalog.yaml"
        path.write_text(yaml.safe_dump(tier_catalog()))
        env_patch = patch.dict(os.environ, {**_CLEAN_ENV, **KEYS})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.hook = build_engine_hook(catalog_path=path, env={**_CLEAN_ENV, **KEYS})
        self.coordinator = self.hook.coordinator

    async def test_chat_pre_call_routes_without_system_prompt_false_positive(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "messages": [
                    {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                    {"role": "user", "content": "Thanks."},
                ],
                "max_completion_tokens": 100,
            },
            call_id="chat-1",
            headers={"x-session-id": "session-1"},
        )

        routed = await self.hook.async_pre_call_hook(None, None, data, "acompletion")

        self.assertEqual(routed["model"], CHAT)
        self.assertEqual(routed["api_key"], ALPHA_KEY)
        self.assertEqual(routed["metadata"]["zhunt"]["tier"], "chat")
        self.assertFalse(routed["metadata"]["zhunt"]["degraded"])

    async def test_responses_pre_call_routes_tool_request(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "instructions": AGENT_SYSTEM_PROMPT,
                "input": "Fix the parser.",
                "tools": [{"type": "function", "name": "shell"}],
                "max_output_tokens": 100,
            },
            call_id="responses-1",
            headers={"x-session-id": "session-2"},
        )

        routed = await self.hook.async_pre_call_hook(None, None, data, "aresponses")

        self.assertEqual(routed["model"], CODING)
        self.assertEqual(routed["api_key"], ALPHA_KEY)
        self.assertEqual(routed["metadata"]["zhunt"]["tier"], "coding")

    async def test_anthropic_pre_call_routes_tool_request(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "system": AGENT_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": "Fix the parser."}],
                "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
                "max_tokens": 100,
            },
            call_id="anthropic-1",
            headers={"x-session-id": "session-3"},
        )

        routed = await self.hook.async_pre_call_hook(None, None, data, "anthropic_messages")

        self.assertEqual(routed["model"], CODING)
        self.assertEqual(routed["api_key"], ALPHA_KEY)
        self.assertEqual(routed["metadata"]["zhunt"]["tier"], "coding")

    async def test_each_dialects_async_call_type_is_routed_not_just_the_sync_one(self) -> None:
        for call_type, body in (
            ("completion", {"model": "zhunt-auto", "messages": [{"role": "user", "content": "x"}]}),
            ("responses", {"model": "zhunt-auto", "input": "x"}),
            ("aanthropic_messages", {"model": "zhunt-auto", "max_tokens": 5,
                                     "messages": [{"role": "user", "content": "x"}]}),
        ):
            data = proxy_data(body, call_id=f"ct-{call_type}")
            routed = await self.hook.async_pre_call_hook(None, None, data, call_type)
            self.assertEqual(routed["model"], CHAT, call_type)

    async def test_explicit_tier_alias_overrides_classification(self) -> None:
        for alias, expected, key in (
            ("zhunt-chat", CHAT, ALPHA_KEY),
            ("zhunt-coding", CODING, ALPHA_KEY),
            ("zhunt-long-context", LONG, BETA_KEY),
            ("zhunt-reasoning", REASONING, BETA_KEY),
        ):
            data = proxy_data(
                {"model": alias, "messages": [{"role": "user", "content": "Hello"}]},
                call_id=f"alias-{alias}",
            )
            routed = await self.hook.async_pre_call_hook(None, None, data, "acompletion")
            self.assertEqual(routed["model"], expected, alias)
            self.assertEqual(routed["api_key"], key, alias)

    async def test_success_resets_escalation_count(self) -> None:
        headers = {"x-session-id": "session-4", "x-zhunt-attempt-id": "attempt-1"}
        body = {"model": "zhunt-auto", "messages": [{"role": "user", "content": "Hello"}]}
        first = proxy_data(body, call_id="call-1", headers=headers)
        await self.hook.async_pre_call_hook(None, None, first, "acompletion")
        await self.hook.async_post_call_success_hook(
            first, None, {"choices": [{"finish_reason": "length"}]}
        )
        self.assertTrue(self.hook.has_retry("attempt-1"))

        retry = proxy_data(body, call_id="call-2", headers=headers)
        await self.hook.async_pre_call_hook(None, None, retry, "acompletion")
        self.assertEqual(retry["metadata"]["zhunt"]["escalation_count"], 1)
        self.assertEqual(retry["metadata"]["zhunt"]["tier"], "coding")
        self.assertEqual(retry["model"], CODING)
        self.assertEqual(retry["metadata"]["zhunt"]["attempt"], 2)
        self.assertFalse(self.hook.has_retry("attempt-1"))

        await self.hook.async_post_call_success_hook(
            retry, None, {"choices": [{"finish_reason": "stop"}]}
        )
        later = self.coordinator.route(
            RoutingRequest(
                model_alias="zhunt-auto",
                user_text="Done",
                first_user_message="Hello",
                session_id="session-4",
                estimated_input_tokens=10,
            )
        )
        self.assertEqual(later.escalation_count, 0)

    async def test_unsupported_call_type_is_unchanged(self) -> None:
        data = {"model": "embedding-model"}

        routed = await self.hook.async_pre_call_hook(None, None, data, "aembedding")

        self.assertIs(routed, data)
        self.assertNotIn("api_key", routed)

    async def test_streaming_hook_escalates_and_clears_pending(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            call_id="stream-1",
            headers={"x-session-id": "session-stream", "x-zhunt-attempt-id": "stream-attempt"},
        )
        await self.hook.async_pre_call_hook(None, None, data, "acompletion")

        with patch.object(
            self.coordinator, "escalate", wraps=self.coordinator.escalate
        ) as escalate:
            streamed = [
                chunk
                async for chunk in self.hook.async_post_call_streaming_iterator_hook(
                    None, stream_of([("length", "partial")], CHAT), data
                )
            ]

        self.assertEqual(len(streamed), 1)
        escalate.assert_called_once()
        self.assertEqual(escalate.call_args.args[1], FailureKind.TRUNCATION)
        self.assertEqual(self.hook._pending, {})
        self.assertTrue(self.hook.has_retry("stream-attempt"))

    async def test_streaming_hook_clears_pending_when_the_client_disconnects(self) -> None:
        data = proxy_data(
            {"model": "zhunt-auto", "messages": [{"role": "user", "content": "Hello"}]},
            call_id="stream-2",
            headers={"x-zhunt-attempt-id": "stream-cancel"},
        )
        await self.hook.async_pre_call_hook(None, None, data, "acompletion")

        stream = self.hook.async_post_call_streaming_iterator_hook(
            None, stream_of([(None, "a"), (None, "b")], CHAT), data
        )
        await stream.__anext__()
        await stream.aclose()  # GeneratorExit, as when the client goes away

        self.assertEqual(self.hook._pending, {})
        self.assertFalse(self.hook.has_retry("stream-cancel"))

    async def test_request_without_an_attempt_id_is_never_queued_for_replay(self) -> None:
        data = proxy_data(
            {"model": "zhunt-auto", "messages": [{"role": "user", "content": "Hello"}]},
            call_id="no-attempt",
        )
        await self.hook.async_pre_call_hook(None, None, data, "acompletion")
        await self.hook.async_post_call_success_hook(
            data, None, {"choices": [{"finish_reason": "length"}]}
        )

        self.assertEqual(self.hook._retries, {})
        self.assertEqual(self.hook._pending, {})


# -- malformed payloads ----------------------------------------------------


def test_malformed_chat_payloads_return_client_errors() -> None:
    call = AsyncMock(return_value=chat_reply())
    with daemon(chat=call) as (client, hook):
        responses = [
            post_chat(client, {"model": 123, "messages": []}),
            post_chat(client, {"messages": []}),
            post_chat(client, {"model": "zhunt-auto"}),
        ]
        pending, decisions = hook._pending, hook.decisions()

    assert [r.status_code for r in responses] == [400] * 3
    assert "payload requires a non-empty model alias" in responses[0].text
    assert "payload requires a non-empty model alias" in responses[1].text
    assert "Chat Completions payload requires a messages list" in responses[2].text
    call.assert_not_awaited()
    assert pending == {} and decisions == []


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        (
            "/v1/responses",
            {"model": "zhunt-auto", "input": 5},
            "Responses payload input must be a string or list",
        ),
        (
            "/v1/responses",
            {"model": "zhunt-auto", "input": "hi", "max_output_tokens": -1},
            "max_output_tokens must be a non-negative integer or null",
        ),
        (
            "/v1/messages",
            {"model": "zhunt-auto", "max_tokens": 5},
            "Anthropic Messages payload requires a messages list",
        ),
        (
            "/v1/messages",
            {"model": "zhunt-auto", "max_tokens": -3,
             "messages": [{"role": "user", "content": "hi"}]},
            "max_tokens must be a non-negative integer",
        ),
    ],
)
def test_malformed_responses_and_messages_payloads_return_client_errors(
    path: str, body: dict, message: str
) -> None:
    chat, responses, messages = (AsyncMock(return_value=chat_reply()) for _ in range(3))
    with daemon(chat=chat, responses=responses, messages=messages) as (client, _):
        response = client.post(path, headers=auth(), json=body)

    assert response.status_code == 400
    assert message in response.text
    for call in (chat, responses, messages):
        call.assert_not_awaited()


def test_tier_whose_provider_has_no_key_degrades_to_one_that_does() -> None:
    call = AsyncMock(return_value=chat_reply())
    # Only alpha has a key, so a reasoning request can only be served lower down.
    with daemon(chat=call, env={"FAKE_API_KEY": ALPHA_KEY}) as (client, _):
        response = post_chat(client, chat_body("Analyze this design", "zhunt-reasoning"))

    assert response.status_code == 200
    assert called_models(call) == [CODING]  # degraded to the next tier down that has a key
    assert call.await_args.kwargs["api_key"] == ALPHA_KEY


# -- classification and rewriting -----------------------------------------


def test_unknown_model_is_classified_and_rewritten() -> None:
    call = AsyncMock(return_value=chat_reply())
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body("Hi", "provider/native"))

    assert response.status_code == 200
    assert call.await_count == 1
    assert call.await_args.kwargs["model"] == CHAT
    assert call.await_args.kwargs["api_key"] == ALPHA_KEY


def test_unknown_model_with_code_in_it_is_classified_to_the_coding_tier() -> None:
    call = AsyncMock(return_value=chat_reply())
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body("Fix this:\n```py\nx = 1\n```", "provider/native"))

    assert response.status_code == 200
    assert called_models(call) == [CODING]


def test_claude_style_model_id_is_classified_and_rewritten() -> None:
    call = AsyncMock(return_value=messages_reply())
    with daemon(messages=call) as (client, _):
        response = post_messages(
            client,
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "ok"
    assert call.await_args.kwargs["model"] == CHAT
    assert call.await_args.kwargs["api_key"] == ALPHA_KEY


def test_responses_unknown_model_is_classified_and_rewritten() -> None:
    call = AsyncMock(return_value=responses_reply())
    with daemon(responses=call) as (client, _):
        response = post_responses(client, {"model": "gpt-5-codex", "input": "Hi"})

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert call.await_args.kwargs["model"] == CHAT
    assert call.await_args.kwargs["api_key"] == ALPHA_KEY


@pytest.mark.parametrize(
    ("alias", "expected", "key"),
    [
        ("zhunt-chat", CHAT, ALPHA_KEY),
        ("zhunt-coding", CODING, ALPHA_KEY),
        ("zhunt-long-context", LONG, BETA_KEY),
        ("zhunt-reasoning", REASONING, BETA_KEY),
    ],
)
def test_chat_endpoint_routes_tier_alias_before_provider_call(
    alias: str, expected: str, key: str
) -> None:
    call = AsyncMock(return_value=chat_reply("Hello"))
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body("Hello", alias))

    assert response.status_code == 200
    assert call.await_args.kwargs["model"] == expected
    assert call.await_args.kwargs["api_key"] == key
    assert first_content(response) == "Hello"


# -- client output caps ----------------------------------------------------


def test_client_length_stops_do_not_pin_session_to_a_higher_tier() -> None:
    call = AsyncMock(return_value=chat_reply("partial", finish_reason="length"))
    with daemon(chat=call) as (client, hook):
        responses = [
            post_chat(client, chat_body(text, max_tokens=1), "cost-session")
            for text in ("hello", "thanks", "hi", "okay")
        ]
        escalations = hook.coordinator._failure_counts

    assert all(r.status_code == 200 for r in responses)
    assert call.await_count == 4
    assert called_models(call) == [CHAT] * 4
    assert escalations == {}


def test_responses_output_cap_keeps_session_cheap() -> None:
    call = AsyncMock(return_value=responses_reply("incomplete"))
    with daemon(responses=call) as (client, _):
        responses = [
            post_responses(
                client,
                {"model": "zhunt-auto", "max_output_tokens": 1, "input": text},
                "responses-capped",
            )
            for text in ("hello", "thanks", "hi", "okay")
        ]

    assert all(r.status_code == 200 for r in responses)
    assert call.await_count == 4
    assert called_models(call) == [CHAT] * 4


def test_anthropic_output_cap_keeps_session_cheap() -> None:
    call = AsyncMock(return_value=messages_reply("partial", stop_reason="max_tokens"))
    with daemon(messages=call) as (client, _):
        responses = [
            post_messages(
                client,
                {
                    "model": "zhunt-auto",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": text}],
                },
                "messages-capped",
            )
            for text in ("hello", "thanks", "hi", "okay")
        ]

    assert all(r.status_code == 200 for r in responses)
    assert call.await_count == 4
    assert called_models(call) == [CHAT] * 4


def test_streaming_length_stop_with_a_client_cap_is_not_retried() -> None:
    call = AsyncMock(side_effect=lambda **_: stream_of([("length", "partial")]))
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body(stream=True, max_tokens=5))
        pending, retries = hook._pending, hook._retries

    assert response.status_code == 200
    assert "partial" in response.text
    assert call.await_count == 1
    assert pending == {} and retries == {}


# -- truncation / refusal promotion ----------------------------------------


def test_uncapped_truncation_promotes_once_per_turn() -> None:
    call = AsyncMock(
        side_effect=[
            chat_reply(finish_reason="length"),  # one, on chat
            chat_reply(finish_reason="length"),  # one, replayed on coding: no 2nd promotion
            chat_reply(),  # two
            chat_reply(),  # three
            chat_reply(),  # four -> third clean turn, so the session decays
            chat_reply(),  # five, back on chat
        ]
    )
    with daemon(chat=call) as (client, _):
        responses = [
            post_chat(client, chat_body(text), "uncapped")
            for text in ("one", "two", "three", "four", "five")
        ]

    assert all(r.status_code == 200 for r in responses)
    assert called_models(call) == [CHAT, CODING, CODING, CODING, CODING, CHAT]


def test_non_streaming_truncation_retries_promoted_model() -> None:
    call = AsyncMock(
        side_effect=[chat_reply("partial", finish_reason="length"), chat_reply("recovered")]
    )
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body(), "retry-session")
        decisions = hook.decisions()

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CHAT, CODING]
    assert first_content(response) == "recovered"
    assert [(d["model"], d["failure"], d["attempt"]) for d in reversed(decisions)] == [
        (CHAT, "truncation", 1),
        (CODING, None, 2),
    ]


def test_escalation_uses_the_key_of_the_provider_it_promotes_to() -> None:
    call = AsyncMock(side_effect=[chat_reply(finish_reason="length"), chat_reply("recovered")])
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body("Hello", "zhunt-coding"))

    assert response.status_code == 200
    assert called_models(call) == [CODING, LONG]
    assert [c.kwargs["api_key"] for c in call.await_args_list] == [ALPHA_KEY, BETA_KEY]


def test_responses_endpoint_retries_incomplete_response() -> None:
    call = AsyncMock(side_effect=[responses_reply("incomplete"), responses_reply("completed")])
    with daemon(responses=call) as (client, _):
        response = post_responses(
            client, {"model": "zhunt-auto", "input": "Hello"}, "responses-retry"
        )

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CHAT, CODING]
    assert response.json()["status"] == "completed"


def test_anthropic_client_max_tokens_stop_is_not_retried() -> None:
    call = AsyncMock(return_value=messages_reply("partial", stop_reason="max_tokens"))
    with daemon(messages=call) as (client, _):
        response = post_messages(
            client,
            {
                "model": "zhunt-auto",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            "anthropic-retry",
        )

    assert response.status_code == 200
    assert call.await_count == 1
    assert called_models(call) == [CHAT]
    assert response.json()["content"][0]["text"] == "partial"


def test_responses_refusal_is_retried_on_the_next_tier() -> None:
    refusal = llm.ModelResponse(dict(
        id="refused",
        created_at=1,
        model="x",
        object="response",
        status="completed",
        output=[
            {
                "type": "message",
                "id": "m",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "refusal", "refusal": "No"}],
            }
        ],
    ))
    call = AsyncMock(side_effect=[refusal, responses_reply("completed")])
    with daemon(responses=call) as (client, _):
        response = post_responses(client, {"model": "zhunt-auto", "input": "Hello"}, "refused")

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]


def test_chat_refusal_is_retried_on_the_next_tier() -> None:
    refused = llm.ModelResponse(dict(
        id="refused",
        model="x",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": None, "refusal": "No"},
            }
        ],
    ))
    call = AsyncMock(side_effect=[refused, chat_reply("answered")])
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body(), "refused")

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]
    assert first_content(response) == "answered"


def test_second_failure_stops_after_one_retry_without_double_promotion() -> None:
    call = AsyncMock(
        side_effect=[
            chat_reply("partial", finish_reason="length"),
            chat_reply("partial", finish_reason="length"),
        ]
    )
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body(), "double-failure")
        pending, retries = hook._pending, hook._retries
        later = hook.coordinator.route(
            RoutingRequest(
                model_alias="zhunt-auto",
                user_text="Continue",
                first_user_message="Hello",
                session_id="double-failure",
                estimated_input_tokens=10,
            )
        )

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CHAT, CODING]  # never reaches long-context or reasoning
    assert pending == {} and retries == {}
    assert later.tier == Tier.CODING
    assert later.escalation_count == 1


def test_a_second_truncated_turn_in_a_row_jumps_straight_to_the_top_tier() -> None:
    call = AsyncMock(
        side_effect=[
            chat_reply(finish_reason="length"),
            chat_reply(finish_reason="length"),
            chat_reply(finish_reason="length"),  # next turn, on the sticky coding tier
            chat_reply("recovered"),
        ]
    )
    with daemon(chat=call) as (client, _):
        first = post_chat(client, chat_body("one"), "turns")
        second = post_chat(client, chat_body("two"), "turns")

    assert first.status_code == second.status_code == 200
    # The failed replay leaves one strike; turn two is truncated on the sticky
    # coding tier, and the second strike skips long-context for reasoning.
    assert called_models(call) == [CHAT, CODING, CODING, REASONING]
    assert first_content(second) == "recovered"


# -- provider errors -------------------------------------------------------


def test_provider_error_degrades_to_the_next_tier_down() -> None:
    call = AsyncMock(side_effect=[RuntimeError("provider unavailable"), chat_reply("recovered")])
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body("Hello", "zhunt-coding"), "error-retry-session")
        decisions = list(reversed(hook.decisions()))
        cooldown = hook.quota.cooldown_remaining((CODING, "default"))

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CODING, CHAT]
    assert first_content(response) == "recovered"
    assert [(d["model"], d["failure"], d["degraded"]) for d in decisions] == [
        (CODING, "server", False),
        (CHAT, None, True),
    ]
    assert cooldown > 0


def test_provider_error_retries_sideways_within_same_tier() -> None:
    catalog = add_models(
        tier_catalog(), "alpha", model("chat-backup", ["chat"], intelligence=20, speed=20)
    )
    call = AsyncMock(
        side_effect=[RuntimeError("provider unavailable"), chat_reply("recovered sideways")]
    )
    with daemon(chat=call, catalog=catalog) as (client, hook):
        response = post_chat(client, chat_body(), "sideways-session")
        benched = hook.quota.cooldown_remaining((CHAT, "default"))

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CHAT, "alpha/chat-backup"]
    assert first_content(response) == "recovered sideways"
    assert benched > 0


def test_a_cooling_down_target_model_is_skipped_on_escalation() -> None:
    catalog = add_models(
        tier_catalog(), "alpha", model("coding-dead", ["coding"], supports_tools=True,
                                       intelligence=100, speed=100)
    )
    call = AsyncMock(side_effect=[chat_reply(finish_reason="length"), chat_reply("recovered")])
    with daemon(chat=call, catalog=catalog) as (client, hook):
        # Control: without the cooldown the stronger model would win the tier.
        assert hook.selector.ranked(Tier.CODING)[0].model.id == "alpha/coding-dead"
        hook.quota.record_failure(("alpha/coding-dead", "default"), ProviderFailure.SERVER)
        response = post_chat(client, chat_body(), "healthy-escalation")

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]
    assert first_content(response) == "recovered"


def test_provider_failure_after_a_promotion_cannot_trigger_a_second_promotion() -> None:
    catalog = add_models(
        tier_catalog(), "alpha", model("code-b", ["coding"], supports_tools=True,
                                       intelligence=50, speed=50)
    )
    call = AsyncMock(
        side_effect=[
            chat_reply(finish_reason="length"),  # chat -> promoted to coding
            RuntimeError("coder down"),  # failover sideways within coding
            chat_reply(finish_reason="length"),  # truncated again: must not promote again
        ]
    )
    with daemon(chat=call, catalog=catalog) as (client, hook):
        response = post_chat(client, chat_body(), "mixed-failures")
        pending, retries = hook._pending, hook._retries

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING, "alpha/code-b"]
    assert pending == {} and retries == {}


def test_failover_is_bounded_by_max_replays() -> None:
    catalog = add_models(
        tier_catalog(),
        "alpha",
        *(model(f"chat-{n}", ["chat"], intelligence=10 + n, speed=10 + n) for n in range(5)),
    )
    call = AsyncMock(side_effect=lambda **_: (_ for _ in ()).throw(RuntimeError("down")))
    with daemon(chat=call, catalog=catalog) as (client, hook):
        response = post_chat(client, chat_body(), "bounded")
        pending, retries = hook._pending, hook._retries
        tried = set(called_models(call))

    # The original attempt plus MAX_REPLAYS failovers, then the error reaches the client.
    assert MAX_REPLAYS == 3
    assert call.await_count == MAX_REPLAYS + 1
    assert len(tried) == MAX_REPLAYS + 1  # every attempt went to a different model
    assert response.status_code >= 500
    assert pending == {} and retries == {}


def test_every_model_failing_in_a_tier_ends_in_503_not_a_loop() -> None:
    call = AsyncMock(side_effect=lambda **_: (_ for _ in ()).throw(RuntimeError("down")))
    with daemon(chat=call) as (client, _):
        response = post_chat(client, chat_body("Hello", "zhunt-coding"))

    assert response.status_code == 503
    assert called_models(call) == [CODING, CHAT]  # coding, then degraded to chat; nothing left


def test_a_client_error_from_the_provider_is_not_retried() -> None:
    bad_request = provider_error(llm.BadRequestError, 400)
    call = AsyncMock(side_effect=[bad_request, chat_reply()])
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body(), "client-error")
        retries = hook._retries

    assert response.status_code == 400
    assert call.await_count == 1
    assert retries == {}


# -- streaming -------------------------------------------------------------


def test_streaming_truncation_escalates_and_clears_pending() -> None:
    attempt = 0

    async def completion(**kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return stream_of([("length", "partial")], kwargs["model"])
        return stream_of([("stop", "recovered")], kwargs["model"])

    call = AsyncMock(side_effect=completion)
    with daemon(chat=call) as (client, hook):
        with patch.object(
            hook.coordinator, "escalate", wraps=hook.coordinator.escalate
        ) as escalate:
            response = post_chat(client, chat_body("Hello", "zhunt-chat", stream=True))
        pending, retries = hook._pending, hook._retries

    assert response.status_code == 200
    assert call.await_count == 2
    assert called_models(call) == [CHAT, CODING]
    assert "partial" not in response.text
    assert "recovered" in response.text
    escalate.assert_called_once()
    assert escalate.call_args.args[1] == FailureKind.TRUNCATION
    assert pending == {} and retries == {}


def test_stream_that_fails_midway_is_replayed_on_another_model() -> None:
    attempt = 0

    async def completion(**kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:

            async def broken():
                yield llm.ModelResponse(dict(
                    id="s", model="x",
                    choices=[{"index": 0, "finish_reason": None, "delta": {"content": "par"}}],
                ))
                raise RuntimeError("connection reset")

            return broken()
        return stream_of([("stop", "recovered")], kwargs["model"])

    call = AsyncMock(side_effect=completion)
    with daemon(chat=call) as (client, hook):
        response = post_chat(client, chat_body("Hello", "zhunt-coding", stream=True))
        pending, retries = hook._pending, hook._retries
        cooldown = hook.quota.cooldown_remaining((CODING, "default"))

    assert response.status_code == 200
    assert called_models(call) == [CODING, CHAT]
    assert "recovered" in response.text
    assert "par" not in response.text.replace("recovered", "")
    assert pending == {} and retries == {}
    assert cooldown > 0


# -- anthropic without a client cap ---------------------------------------


def test_anthropic_max_tokens_stop_without_a_client_cap_is_retried() -> None:
    call = AsyncMock(
        side_effect=[messages_reply("partial", stop_reason="max_tokens"), messages_reply("full")]
    )
    with daemon(messages=call) as (client, _):
        response = client.post(
            "/v1/messages",
            headers=auth("uncapped-messages"),
            json={"model": "zhunt-auto", "messages": [{"role": "user", "content": "Hello"}]},
        )

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]
    assert response.json()["content"][0]["text"] == "full"


# -- telemetry -------------------------------------------------------------


def telemetry_catalog() -> dict[str, Any]:
    data = tier_catalog()
    data["providers"]["beta"]["models"][-1] = model(
        "reason-m", ["reasoning"], supports_tools=True, **{"in": 5.0, "out": 10.0}
    )
    return data


def test_successful_request_writes_local_telemetry() -> None:
    call = AsyncMock(return_value=chat_reply(usage={"prompt_tokens": 2, "completion_tokens": 3}))
    with TemporaryDirectory() as directory:
        path = Path(directory) / "telemetry.jsonl"
        with daemon(chat=call, catalog=telemetry_catalog(), telemetry_path=path) as (client, _):
            response = post_chat(client, chat_body("Hi", "provider/native"))
        event = json.loads(path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert event["wire_dialect"] == "openai-chat-completions"
    assert event["model"] == CHAT
    assert event["tier"] == "chat"
    assert event["input_tokens"] == 2
    assert event["output_tokens"] == 3
    assert event["actual_cost"] == 0.0  # a free model
    assert event["counterfactual_top_model_cost"] == 0.00004
    assert event["success"] is True
    assert "failure" not in event


def test_truncation_and_its_retry_are_both_recorded_in_telemetry() -> None:
    call = AsyncMock(side_effect=[chat_reply(finish_reason="length"), chat_reply("recovered")])
    with TemporaryDirectory() as directory:
        path = Path(directory) / "telemetry.jsonl"
        with daemon(chat=call, telemetry_path=path) as (client, _):
            response = post_chat(client, chat_body())
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert response.status_code == 200
    assert [(e["model"], e["success"], e.get("failure")) for e in events] == [
        (CHAT, False, "truncation"),
        (CODING, True, None),
    ]


@pytest.mark.parametrize(
    ("dialect", "path", "body", "reply"),
    [
        ("openai-chat-completions", "/v1/chat/completions", chat_body(), "chat"),
        ("openai-responses", "/v1/responses", {"model": "zhunt-auto", "input": "Hi"}, "responses"),
        (
            "anthropic-messages",
            "/v1/messages",
            {"model": "zhunt-auto", "max_tokens": 5,
             "messages": [{"role": "user", "content": "Hi"}]},
            "messages",
        ),
    ],
)
def test_telemetry_labels_each_wire_dialect(dialect: str, path: str, body: dict, reply: str) -> None:
    mocks = {
        "chat": AsyncMock(return_value=chat_reply()),
        "responses": AsyncMock(return_value=responses_reply()),
        "messages": AsyncMock(return_value=messages_reply()),
    }
    with TemporaryDirectory() as directory:
        telemetry = Path(directory) / "telemetry.jsonl"
        with daemon(**mocks, telemetry_path=telemetry) as (client, _):
            response = client.post(path, headers=auth(), json=body)
        event = json.loads(telemetry.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert event["wire_dialect"] == dialect
    mocks[reply].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
