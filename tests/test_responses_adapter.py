import unittest

from zhunt.adapters import AdapterError, OpenAIResponsesAdapter
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import RoutingCoordinator


class OpenAIResponsesAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIResponsesAdapter()
        self.payload = {
            "model": "zhunt-auto",
            "instructions": "Work carefully.",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": "Follow repository conventions.",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Start the task."}
                    ],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "failed",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Fix the implementation."}
                    ],
                },
            ],
            "tools": [{"type": "function", "name": "shell"}],
            "conversation": "conv_123",
            "max_output_tokens": 1_024,
        }

    def test_normalizes_responses_items(self) -> None:
        request = self.adapter.normalize(self.payload)

        self.assertEqual(request.model_alias, "zhunt-auto")
        self.assertEqual(
            request.system_prompt,
            "Work carefully.\nFollow repository conventions.",
        )
        self.assertEqual(request.first_user_message, "Start the task.")
        self.assertEqual(request.user_text, "Fix the implementation.")
        self.assertEqual(request.session_id, "conv_123")
        self.assertEqual(request.estimated_output_tokens, 1_024)
        self.assertTrue(request.has_tool_calls)

    def test_string_input_is_a_user_message(self) -> None:
        request = self.adapter.normalize(
            {
                "model": "zhunt-chat",
                "input": "Hello",
                "previous_response_id": "resp_123",
            }
        )

        self.assertEqual(request.first_user_message, "Hello")
        self.assertEqual(request.user_text, "Hello")
        self.assertEqual(request.session_id, "resp_123")

    def test_session_header_takes_precedence(self) -> None:
        request = self.adapter.normalize(
            self.payload,
            headers={"x-conversation-id": "header-conversation"},
        )

        self.assertEqual(request.session_id, "header-conversation")

    def test_metadata_session_precedes_previous_response(self) -> None:
        request = self.adapter.normalize(
            {
                "model": "zhunt-chat",
                "input": "Hello",
                "metadata": {"session_id": "metadata-session"},
                "previous_response_id": "resp_123",
            }
        )

        self.assertEqual(request.session_id, "metadata-session")

    def test_route_invokes_core_and_rewrites_copy(self) -> None:
        coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(
                {
                    "aliases": {"zhunt-auto": {"tier": "auto"}},
                    "tiers": {
                        "coding": [
                            {"model": "provider/coding", "in": 1, "out": 2}
                        ]
                    },
                }
            ),
            classifier=HeuristicClassifier(long_context_tokens=100_000),
        )

        routed = self.adapter.route(self.payload, coordinator)

        self.assertEqual(routed.decision.tier, Tier.CODING)
        self.assertEqual(routed.upstream_payload["model"], "provider/coding")
        self.assertEqual(self.payload["model"], "zhunt-auto")

    def test_invalid_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "string or list"):
            self.adapter.normalize({"model": "zhunt-auto", "input": 42})

    def test_invalid_max_output_tokens_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "max_output_tokens"):
            self.adapter.normalize(
                {
                    "model": "zhunt-auto",
                    "input": "Hello",
                    "max_output_tokens": "many",
                }
            )


if __name__ == "__main__":
    unittest.main()
