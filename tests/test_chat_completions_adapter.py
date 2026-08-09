import unittest

from zhunt.adapters import AdapterError, OpenAIChatCompletionsAdapter
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import RoutingCoordinator


class OpenAIChatCompletionsAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = OpenAIChatCompletionsAdapter()
        self.payload = {
            "model": "zhunt-auto",
            "messages": [
                {"role": "system", "content": "You are concise."},
                {
                    "role": "developer",
                    "content": [{"type": "text", "text": "Follow repo rules."}],
                },
                {"role": "user", "content": "Start the task."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "failed"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Fix the implementation."}
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "shell", "parameters": {}},
                }
            ],
            "max_completion_tokens": 2_048,
            "metadata": {"session_id": "chat-session"},
        }

    def test_normalizes_chat_messages(self) -> None:
        request = self.adapter.normalize(self.payload)

        self.assertEqual(request.model_alias, "zhunt-auto")
        self.assertEqual(
            request.system_prompt,
            "You are concise.\nFollow repo rules.",
        )
        self.assertEqual(request.first_user_message, "Start the task.")
        self.assertEqual(request.user_text, "Fix the implementation.")
        self.assertEqual(request.session_id, "chat-session")
        self.assertEqual(request.estimated_output_tokens, 2_048)
        self.assertTrue(request.has_tool_calls)

    def test_legacy_max_tokens_is_supported(self) -> None:
        request = self.adapter.normalize(
            {
                "model": "zhunt-chat",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 128,
            }
        )

        self.assertEqual(request.estimated_output_tokens, 128)

    def test_session_header_takes_precedence(self) -> None:
        request = self.adapter.normalize(
            self.payload,
            headers={"X-Session-ID": "header-session"},
        )

        self.assertEqual(request.session_id, "header-session")

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

    def test_apply_route_moves_mid_conversation_system_items_to_the_front(
        self,
    ) -> None:
        # Same fix as the Responses adapter, same root cause: Anthropic-family
        # backends reject a system/developer message that isn't first, even
        # though OpenAI's Chat Completions format allows it anywhere.
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
        payload = {
            "model": "zhunt-auto",
            "messages": [
                {"role": "user", "content": "Start the task."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "failed"},
                {"role": "system", "content": "Reminder: follow the style guide."},
                {"role": "user", "content": "Try again."},
            ],
        }

        routed = self.adapter.route(payload, coordinator)

        self.assertEqual(
            [message["role"] for message in routed.upstream_payload["messages"]],
            ["system", "user", "assistant", "tool", "user"],
        )
        self.assertEqual(
            routed.upstream_payload["messages"][1]["content"], "Start the task."
        )
        self.assertEqual(
            routed.upstream_payload["messages"][4]["content"], "Try again."
        )
        self.assertEqual(payload["messages"][0]["role"], "user")

    def test_missing_messages_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "messages list"):
            self.adapter.normalize({"model": "zhunt-auto"})

    def test_invalid_max_tokens_is_rejected(self) -> None:
        with self.assertRaisesRegex(AdapterError, "max_completion_tokens"):
            self.adapter.normalize(
                {
                    "model": "zhunt-auto",
                    "messages": [],
                    "max_tokens": "many",
                }
            )


if __name__ == "__main__":
    unittest.main()
