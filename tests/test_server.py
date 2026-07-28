import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import litellm
from fastapi.testclient import TestClient

from tests.representative_prompts import AGENT_SYSTEM_PROMPT
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import FailureKind, RoutingCoordinator, RoutingRequest
from zhunt.server import ZhuntProxyHook, _response_failure, create_proxy_app


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


class ResponseFailureTests(unittest.TestCase):
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

    def test_normal_response_has_no_failure(self) -> None:
        self.assertIsNone(
            _response_failure({"choices": [{"finish_reason": "stop"}]})
        )


class LiteLLMProxyIntegrationTests(unittest.TestCase):
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
                    create_proxy_app(registry_path=registry_path)
                ) as client:
                    response = client.post(
                        "/v1/chat/completions",
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


if __name__ == "__main__":
    unittest.main()
