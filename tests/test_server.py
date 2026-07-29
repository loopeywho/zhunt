import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import litellm
from fastapi.testclient import TestClient
from litellm.proxy import proxy_server
from litellm.types.llms.anthropic_messages.anthropic_response import (
    AnthropicMessagesResponse,
)
from litellm.types.llms.openai import ResponsesAPIResponse

from tests.representative_prompts import AGENT_SYSTEM_PROMPT
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import FailureKind, RoutingCoordinator, RoutingRequest
from zhunt.server import ZhuntProxyHook, _response_failure, create_proxy_app


def auth_headers(**extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {proxy_server.master_key}",
        **extra,
    }


REGISTRY = {
    "aliases": {
        "zhunt-auto": {"tier": "auto"},
        "zhunt-chat": {"tier": "chat"},
    },
    "tiers": {
        "chat": [{"model": "provider/chat", "in": 0.1, "out": 0.2}],
        "coding": [{"model": "provider/coding", "in": 0.2, "out": 0.4}],
        "long-context": [{"model": "provider/long", "in": 0.3, "out": 0.6}],
        "reasoning": [{"model": "provider/reasoning", "in": 1, "out": 2}],
    },
}


def proxy_data(
    body: dict,
    *,
    call_id: str,
    headers: dict[str, str] | None = None,
) -> dict:
    return {
        "model": body["model"],
        "litellm_call_id": call_id,
        "proxy_server_request": {
            "body": body,
            "headers": headers or {},
        },
    }


class ZhuntProxyHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(REGISTRY),
            classifier=HeuristicClassifier(),
        )
        self.hook = ZhuntProxyHook(self.coordinator)

    async def test_chat_pre_call_routes_without_system_prompt_false_positive(
        self,
    ) -> None:
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

        routed = await self.hook.async_pre_call_hook(
            None,
            None,
            data,
            "acompletion",
        )

        self.assertEqual(routed["model"], "provider/chat")
        self.assertEqual(routed["metadata"]["zhunt"]["tier"], "chat")

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

        routed = await self.hook.async_pre_call_hook(
            None,
            None,
            data,
            "aresponses",
        )

        self.assertEqual(routed["model"], "provider/coding")
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

        routed = await self.hook.async_pre_call_hook(
            None,
            None,
            data,
            "anthropic_messages",
        )

        self.assertEqual(routed["model"], "provider/coding")
        self.assertEqual(routed["metadata"]["zhunt"]["tier"], "coding")

    async def test_success_resets_failure_count(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            call_id="call-1",
            headers={"x-session-id": "session-4"},
        )
        await self.hook.async_pre_call_hook(None, None, data, "acompletion")
        await self.hook.async_post_call_failure_hook(
            data,
            RuntimeError("provider failed"),
            None,
        )

        retry_data = proxy_data(
            {
                "model": "zhunt-auto",
                "messages": [{"role": "user", "content": "Continue"}],
            },
            call_id="call-2",
            headers={"x-session-id": "session-4"},
        )
        await self.hook.async_pre_call_hook(
            None,
            None,
            retry_data,
            "acompletion",
        )
        self.assertEqual(
            retry_data["metadata"]["zhunt"]["escalation_count"],
            1,
        )

        await self.hook.async_post_call_success_hook(
            retry_data,
            None,
            {"choices": [{"finish_reason": "stop"}]},
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

        routed = await self.hook.async_pre_call_hook(
            None,
            None,
            data,
            "aembedding",
        )

        self.assertIs(routed, data)

    async def test_streaming_hook_escalates_and_clears_pending(self) -> None:
        data = proxy_data(
            {
                "model": "zhunt-auto",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            call_id="stream-1",
            headers={"x-session-id": "session-stream"},
        )
        await self.hook.async_pre_call_hook(None, None, data, "acompletion")

        async def chunks():
            yield litellm.ModelResponseStream(
                id="stream-response-1",
                model="provider/chat",
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "delta": {"content": "partial"},
                    }
                ],
            )

        with patch.object(
            self.coordinator,
            "escalate",
            wraps=self.coordinator.escalate,
        ) as escalate:
            streamed = [
                chunk
                async for chunk in (
                    self.hook.async_post_call_streaming_iterator_hook(
                        None,
                        chunks(),
                        data,
                    )
                )
            ]

        self.assertEqual(len(streamed), 1)
        escalate.assert_called_once()
        self.assertEqual(
            escalate.call_args.args[1],
            FailureKind.TRUNCATION,
        )
        self.assertEqual(self.hook._pending, {})


class ResponseFailureTests(unittest.TestCase):
    def test_client_output_cap_makes_length_stop_routine(self) -> None:
        self.assertIsNone(
            _response_failure(
                {"choices": [{"finish_reason": "length"}]},
                request_data={"max_tokens": 1},
            )
        )

    def test_detects_chat_truncation(self) -> None:
        self.assertEqual(
            _response_failure({"choices": [{"finish_reason": "length"}]}),
            FailureKind.TRUNCATION,
        )

    def test_detects_responses_refusal(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "No"}],
                        }
                    ]
                }
            ),
            FailureKind.REFUSAL,
        )

    def test_detects_anthropic_stream_truncation(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "max_tokens"},
                }
            ),
            FailureKind.TRUNCATION,
        )

    def test_detects_responses_stream_truncation(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "type": "response.incomplete",
                    "response": {"status": "incomplete"},
                }
            ),
            FailureKind.TRUNCATION,
        )

    def test_normal_response_has_no_failure(self) -> None:
        self.assertIsNone(
            _response_failure({"choices": [{"finish_reason": "stop"}]})
        )


class LiteLLMProxyIntegrationTests(unittest.TestCase):
    def test_malformed_chat_payloads_return_client_errors(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
"""
        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            app = create_proxy_app(
                registry_path=registry_path,
                env_path=Path(directory) / "zhunt.env",
            )
            with TestClient(app) as client:
                responses = [
                    client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(),
                        json={"model": 123, "messages": []},
                    ),
                    client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(),
                        json={"messages": []},
                    ),
                    client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(),
                        json={"model": "zhunt-auto"},
                    ),
                ]

        self.assertEqual([response.status_code for response in responses], [400] * 3)
        self.assertIn(
            "payload requires a non-empty model alias",
            responses[0].text,
        )
        self.assertIn(
            "Chat Completions payload requires a messages list",
            responses[2].text,
        )

    def test_unknown_model_passthrough_is_not_a_server_error(self) -> None:
        registry_yaml = """\
aliases: {}
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
"""
        provider_response = litellm.ModelResponse(
            id="passthrough-response",
            model="provider/native",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        )
        provider_call = AsyncMock(return_value=provider_response)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(),
                        json={
                            "model": "provider/native",
                            "messages": [{"role": "user", "content": "Hi"}],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 1)
        self.assertEqual(provider_call.await_args.kwargs["model"], "provider/native")

    def test_client_length_stops_do_not_pin_session_to_expensive_tier(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""
        provider_response = litellm.ModelResponse(
            id="response-length",
            model="provider/test-chat",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial"},
                }
            ],
        )
        provider_call = AsyncMock(return_value=provider_response)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                with TestClient(app) as client:
                    responses = [
                        client.post(
                            "/v1/chat/completions",
                            headers=auth_headers(**{"x-session-id": "cost-session"}),
                            json={
                                "model": "zhunt-auto",
                                "max_tokens": 1,
                                "messages": [{"role": "user", "content": text}],
                            },
                        )
                        for text in ("hello", "thanks", "hi", "okay")
                    ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(provider_call.await_count, 4)
        self.assertEqual(
            [call.kwargs["model"] for call in provider_call.await_args_list],
            ["provider/test-chat"] * 4,
        )

    def test_uncapped_truncation_promotes_once_per_turn(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
  reasoning:
    - model: provider/test-reasoning
      in: 1
      out: 2
"""

        def response(identifier: str, model: str, finish_reason: str):
            return litellm.ModelResponse(
                id=identifier,
                model=model,
                choices=[
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            )

        provider_call = AsyncMock(
            side_effect=[
                response("one-chat", "provider/test-chat", "length"),
                response("one-coding", "provider/test-coding", "length"),
                response("two", "provider/test-coding", "stop"),
                response("three", "provider/test-coding", "stop"),
                response("four", "provider/test-coding", "stop"),
                response("five", "provider/test-chat", "stop"),
            ]
        )

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                with TestClient(app) as client:
                    responses = [
                        client.post(
                            "/v1/chat/completions",
                            headers=auth_headers(**{"x-session-id": "uncapped"}),
                            json={
                                "model": "zhunt-auto",
                                "messages": [{"role": "user", "content": text}],
                            },
                        )
                        for text in ("one", "two", "three", "four", "five")
                    ]

        self.assertTrue(all(item.status_code == 200 for item in responses))
        self.assertEqual(
            [call.kwargs["model"] for call in provider_call.await_args_list],
            [
                "provider/test-chat",
                "provider/test-coding",
                "provider/test-coding",
                "provider/test-coding",
                "provider/test-coding",
                "provider/test-chat",
            ],
        )

    def test_responses_output_cap_keeps_session_cheap(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  reasoning:
    - model: provider/test-reasoning
      in: 1
      out: 2
"""
        incomplete = ResponsesAPIResponse(
            id="capped-response",
            created_at=1,
            model="provider/test-chat",
            object="response",
            output=[],
            status="incomplete",
        )
        provider_call = AsyncMock(return_value=incomplete)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.aresponses", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                with TestClient(app) as client:
                    responses = [
                        client.post(
                            "/v1/responses",
                            headers=auth_headers(**{"x-session-id": "responses-capped"}),
                            json={
                                "model": "zhunt-auto",
                                "max_output_tokens": 1,
                                "input": text,
                            },
                        )
                        for text in ("hello", "thanks", "hi", "okay")
                    ]

        self.assertTrue(all(item.status_code == 200 for item in responses))
        self.assertEqual(provider_call.await_count, 4)
        self.assertEqual(
            [call.kwargs["model"] for call in provider_call.await_args_list],
            ["provider/test-chat"] * 4,
        )

    def test_anthropic_output_cap_keeps_session_cheap(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  reasoning:
    - model: provider/test-reasoning
      in: 1
      out: 2
"""
        capped = AnthropicMessagesResponse(
            id="capped-message",
            type="message",
            role="assistant",
            model="provider/test-chat",
            content=[{"type": "text", "text": "partial"}],
            stop_reason="max_tokens",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        provider_call = AsyncMock(return_value=capped)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.anthropic_messages", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                with TestClient(app) as client:
                    responses = [
                        client.post(
                            "/v1/messages",
                            headers=auth_headers(**{"x-session-id": "messages-capped"}),
                            json={
                                "model": "zhunt-auto",
                                "max_tokens": 1,
                                "messages": [{"role": "user", "content": text}],
                            },
                        )
                        for text in ("hello", "thanks", "hi", "okay")
                    ]

        self.assertTrue(all(item.status_code == 200 for item in responses))
        self.assertEqual(provider_call.await_count, 4)
        self.assertEqual(
            [call.kwargs["model"] for call in provider_call.await_args_list],
            ["provider/test-chat"] * 4,
        )

    def test_chat_endpoint_routes_before_provider_call(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-chat:
    tier: chat
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
"""
        provider_response = litellm.ModelResponse(
            id="response-1",
            model="provider/test-chat",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Hello"},
                }
            ],
        )
        provider_call = AsyncMock(return_value=provider_response)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                with TestClient(
                    create_proxy_app(
                        registry_path=registry_path,
                        env_path=Path(directory) / "zhunt.env",
                    )
                ) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(),
                        json={
                            "model": "zhunt-chat",
                            "messages": [
                                {"role": "user", "content": "Hello"}
                            ],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            provider_call.await_args.kwargs["model"],
            "provider/test-chat",
        )

    def test_non_streaming_truncation_retries_promoted_model(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""
        truncated = litellm.ModelResponse(
            id="response-1",
            model="provider/test-chat",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "partial",
                    },
                }
            ],
        )
        recovered = litellm.ModelResponse(
            id="response-2",
            model="provider/test-coding",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "recovered",
                    },
                }
            ],
        )
        provider_call = AsyncMock(side_effect=[truncated, recovered])

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                with TestClient(
                    create_proxy_app(
                        registry_path=registry_path,
                        env_path=Path(directory) / "zhunt.env",
                    )
                ) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(**{"x-session-id": "retry-session"}),
                        json={
                            "model": "zhunt-auto",
                            "messages": [
                                {"role": "user", "content": "Hello"}
                            ],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 2)
        self.assertEqual(
            provider_call.await_args_list[0].kwargs["model"],
            "provider/test-chat",
        )
        self.assertEqual(
            provider_call.await_args_list[1].kwargs["model"],
            "provider/test-coding",
        )
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "recovered",
        )

    def test_provider_error_retries_promoted_model(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""
        recovered = litellm.ModelResponse(
            id="response-2",
            model="provider/test-coding",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "recovered",
                    },
                }
            ],
        )
        provider_call = AsyncMock(
            side_effect=[RuntimeError("provider unavailable"), recovered]
        )

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                with TestClient(
                    create_proxy_app(
                        registry_path=registry_path,
                        env_path=Path(directory) / "zhunt.env",
                    )
                ) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(**{"x-session-id": "error-retry-session"}),
                        json={
                            "model": "zhunt-auto",
                            "messages": [
                                {"role": "user", "content": "Hello"}
                            ],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 2)
        self.assertEqual(
            provider_call.await_args_list[0].kwargs["model"],
            "provider/test-chat",
        )
        self.assertEqual(
            provider_call.await_args_list[1].kwargs["model"],
            "provider/test-coding",
        )
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "recovered",
        )

    def test_second_failure_stops_after_one_retry_without_double_promotion(
        self,
    ) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
  reasoning:
    - model: provider/test-reasoning
      in: 1
      out: 2
"""

        def truncated_response(identifier: str, model: str):
            return litellm.ModelResponse(
                id=identifier,
                model=model,
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "partial",
                        },
                    }
                ],
            )

        provider_call = AsyncMock(
            side_effect=[
                truncated_response("response-1", "provider/test-chat"),
                truncated_response("response-2", "provider/test-coding"),
            ]
        )
        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                hook = litellm.callbacks[0]
                with TestClient(app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        headers=auth_headers(**{"x-session-id": "double-failure"}),
                        json={
                            "model": "zhunt-auto",
                            "messages": [
                                {"role": "user", "content": "Hello"}
                            ],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 2)
        self.assertEqual(hook._pending, {})
        self.assertEqual(hook._retry_routes, {})
        later = hook.coordinator.route(
            RoutingRequest(
                model_alias="zhunt-auto",
                user_text="Continue",
                first_user_message="Hello",
                session_id="double-failure",
                estimated_input_tokens=10,
            )
        )
        self.assertEqual(later.tier, Tier.CODING)
        self.assertEqual(later.escalation_count, 1)

    def test_responses_endpoint_retries_incomplete_response(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""
        incomplete = ResponsesAPIResponse(
            id="response-1",
            created_at=1,
            model="provider/test-chat",
            object="response",
            output=[],
            status="incomplete",
        )
        recovered = ResponsesAPIResponse(
            id="response-2",
            created_at=2,
            model="provider/test-coding",
            object="response",
            output=[],
            status="completed",
        )
        provider_call = AsyncMock(side_effect=[incomplete, recovered])

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.aresponses", provider_call):
                with TestClient(
                    create_proxy_app(
                        registry_path=registry_path,
                        env_path=Path(directory) / "zhunt.env",
                    )
                ) as client:
                    response = client.post(
                        "/v1/responses",
                        headers=auth_headers(**{"x-session-id": "responses-retry"}),
                        json={
                            "model": "zhunt-auto",
                            "input": "Hello",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 2)
        self.assertEqual(
            provider_call.await_args_list[0].kwargs["model"],
            "provider/test-chat",
        )
        self.assertEqual(
            provider_call.await_args_list[1].kwargs["model"],
            "provider/test-coding",
        )
        self.assertEqual(response.json()["status"], "completed")

    def test_anthropic_client_max_tokens_stop_is_not_retried(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-auto:
    tier: auto
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""
        truncated = AnthropicMessagesResponse(
            id="message-1",
            type="message",
            role="assistant",
            model="provider/test-chat",
            content=[{"type": "text", "text": "partial"}],
            stop_reason="max_tokens",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        provider_call = AsyncMock(return_value=truncated)

        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.anthropic_messages", provider_call):
                with TestClient(
                    create_proxy_app(
                        registry_path=registry_path,
                        env_path=Path(directory) / "zhunt.env",
                    )
                ) as client:
                    response = client.post(
                        "/v1/messages",
                        headers=auth_headers(**{"x-session-id": "anthropic-retry"}),
                        json={
                            "model": "zhunt-auto",
                            "max_tokens": 10,
                            "messages": [
                                {"role": "user", "content": "Hello"}
                            ],
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 1)
        self.assertEqual(
            provider_call.await_args_list[0].kwargs["model"],
            "provider/test-chat",
        )
        self.assertEqual(response.json()["content"][0]["text"], "partial")

    def test_streaming_truncation_escalates_and_clears_pending(self) -> None:
        registry_yaml = """\
aliases:
  zhunt-chat:
    tier: chat
tiers:
  chat:
    - model: provider/test-chat
      in: 0.1
      out: 0.2
  coding:
    - model: provider/test-coding
      in: 0.2
      out: 0.4
"""

        attempt = 0

        async def completion(**kwargs):
            nonlocal attempt
            attempt += 1

            async def provider_stream():
                yield litellm.ModelResponseStream(
                    id=f"stream-response-{attempt}",
                    model=kwargs["model"],
                    choices=[
                        {
                            "index": 0,
                            "finish_reason": (
                                "length" if attempt == 1 else "stop"
                            ),
                            "delta": {
                                "content": (
                                    "partial"
                                    if attempt == 1
                                    else "recovered"
                                )
                            },
                        }
                    ],
                )

            return provider_stream()

        provider_call = AsyncMock(side_effect=completion)
        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(registry_yaml, encoding="utf-8")
            with patch("litellm.acompletion", provider_call):
                app = create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "zhunt.env",
                )
                hook = litellm.callbacks[0]
                with patch.object(
                    hook.coordinator,
                    "escalate",
                    wraps=hook.coordinator.escalate,
                ) as escalate:
                    with TestClient(app) as client:
                        response = client.post(
                            "/v1/chat/completions",
                            headers=auth_headers(),
                            json={
                                "model": "zhunt-chat",
                                "stream": True,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "Hello",
                                    }
                                ],
                            },
                        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_call.await_count, 2)
        self.assertEqual(
            provider_call.await_args_list[0].kwargs["model"],
            "provider/test-chat",
        )
        self.assertEqual(
            provider_call.await_args_list[1].kwargs["model"],
            "provider/test-coding",
        )
        self.assertNotIn("partial", response.text)
        self.assertIn("recovered", response.text)
        escalate.assert_called_once()
        self.assertEqual(
            escalate.call_args.args[1],
            FailureKind.TRUNCATION,
        )
        self.assertEqual(hook._pending, {})
        self.assertEqual(hook._retry_routes, {})


if __name__ == "__main__":
    unittest.main()
