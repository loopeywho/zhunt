import unittest

from zhunt.adapters import AdapterError, AnthropicMessagesAdapter
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import RoutingCoordinator


class AnthropicMessagesAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AnthropicMessagesAdapter()
        self.payload = {
            "model": "zhunt-auto",
            "system": [{"type": "text", "text": "You are a coding agent."}],
            "messages": [
                {"role": "user", "content": "Start the task."},
                {"role": "assistant", "content": "Working."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Fix this function."},
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "failure",
                        },
                    ],
                },
            ],
            "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
            "max_tokens": 512,
            "metadata": {"session_id": "metadata-session"},
        }

    def test_normalizes_anthropic_messages(self) -> None:
        request = self.adapter.normalize(
            self.payload,
            headers={"X-Session-ID": "header-session"},
        )

        self.assertEqual(request.model_alias, "zhunt-auto")
        self.assertEqual(request.system_prompt, "You are a coding agent.")
        self.assertEqual(request.first_user_message, "Start the task.")
        self.assertEqual(request.user_text, "Fix this function.")
        self.assertEqual(request.session_id, "header-session")
        self.assertEqual(request.estimated_output_tokens, 512)
        self.assertTrue(request.has_tool_calls)

    def test_metadata_session_is_used_when_header_is_absent(self) -> None:
        request = self.adapter.normalize(self.payload)

        self.assertEqual(request.session_id, "metadata-session")

    def test_route_invokes_core_and_rewrites_copy(self) -> None:
        registry = ModelRegistry.from_data(
            {
                "aliases": {"zhunt-auto": {"tier": "auto"}},
                "tiers": {
                    "coding": [
                        {"model": "provider/coding", "in": 1, "out": 2}
                    ]
                },
            }
        )
        coordinator = RoutingCoordinator(
            registry=registry,
            classifier=HeuristicClassifier(long_context_tokens=100_000),
        )

        routed = self.adapter.route(self.payload, coordinator)

        self.assertEqual(routed.decision.tier, Tier.CODING)
        self.assertEqual(routed.upstream_payload["model"], "provider/coding")
        self.assertEqual(self.payload["model"], "zhunt-auto")

    def test_missing_messages_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "messages list"):
            self.adapter.normalize({"model": "zhunt-auto"})

    def test_missing_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "model alias"):
            self.adapter.normalize({"messages": []})

    def test_invalid_max_tokens_is_rejected(self) -> None:
        payload = {**self.payload, "max_tokens": "many"}

        with self.assertRaisesRegex(AdapterError, "max_tokens"):
            self.adapter.normalize(payload)


if __name__ == "__main__":
    unittest.main()
