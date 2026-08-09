import unittest

from zhunt.adapters import AdapterError, OpenAIResponsesAdapter
from zhunt.adapters.base import strip_thinking_blocks
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

    def test_apply_route_moves_mid_conversation_system_items_to_the_front(
        self,
    ) -> None:
        # Anthropic-family backends reject a system/developer item that isn't
        # first (reproduced live 2026-08-09 against
        # openrouter/anthropic/claude-sonnet-5, identical failure across four
        # backend infra providers) even though it's valid per the Responses
        # API's own spec. apply_route is the boundary that must fix this,
        # regardless of which model ends up routed to.
        coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(
                {
                    "aliases": {"zhunt-auto": {"tier": "auto"}},
                    "tiers": {
                        "chat": [{"model": "provider/chat", "in": 1, "out": 2}]
                    },
                }
            ),
            classifier=HeuristicClassifier(),
        )
        payload = {
            "model": "zhunt-auto",
            "input": [
                {"type": "message", "role": "user", "content": "Please help."},
                {
                    "type": "message",
                    "role": "system",
                    "content": "Reminder: follow the style guide.",
                },
                {"type": "message", "role": "user", "content": "Go ahead."},
            ],
        }

        routed = self.adapter.route(payload, coordinator)

        self.assertEqual(
            [item["role"] for item in routed.upstream_payload["input"]],
            ["system", "user", "user"],
        )
        # Relative order within each group is preserved, not just the split.
        self.assertEqual(
            routed.upstream_payload["input"][1]["content"], "Please help."
        )
        self.assertEqual(
            routed.upstream_payload["input"][2]["content"], "Go ahead."
        )
        # The original payload passed in is untouched.
        self.assertEqual(payload["input"][0]["role"], "user")

    def test_apply_route_leaves_string_input_untouched(self) -> None:
        coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(
                {
                    "aliases": {"zhunt-auto": {"tier": "auto"}},
                    "tiers": {
                        "chat": [{"model": "provider/chat", "in": 1, "out": 2}]
                    },
                }
            ),
            classifier=HeuristicClassifier(),
        )

        routed = self.adapter.route(
            {"model": "zhunt-auto", "input": "Hello"}, coordinator
        )

        self.assertEqual(routed.upstream_payload["input"], "Hello")

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


class StripThinkingBlocksTests(unittest.TestCase):
    """Direct unit tests for strip_thinking_blocks (no adapter/router)."""

    def _msg(self, role: str, content: list) -> dict:
        return {"type": "message", "role": role, "content": content}

    def _text_block(self, text: str = "hello") -> dict:
        return {"type": "text", "text": text}

    def _thinking_block(self, signature: str = "sig1") -> dict:
        return {"type": "thinking", "thinking": "...", "signature": signature}

    def _redacted_block(self, data: str = "AAAA") -> dict:
        return {"type": "redacted_thinking", "data": data}

    def test_passthrough_string_content(self) -> None:
        """Messages with string content pass through unchanged."""
        items = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = strip_thinking_blocks(items)
        self.assertEqual(result, items)

    def test_passthrough_no_thinking(self) -> None:
        """Messages with only text blocks pass through unchanged."""
        items = [
            self._msg("user", [self._text_block("hello")]),
            self._msg("assistant", [self._text_block("world")]),
        ]
        result = strip_thinking_blocks(items)
        self.assertEqual(result, items)

    def test_strips_thinking_block(self) -> None:
        """A thinking block is removed from assistant content."""
        items = [
            self._msg("assistant", [self._thinking_block()]),
        ]
        result = strip_thinking_blocks(items)
        self.assertEqual(result, [self._msg("assistant", [])])

    def test_strips_redacted_thinking_block(self) -> None:
        """A redacted_thinking block is removed from assistant content."""
        items = [
            self._msg("assistant", [self._redacted_block()]),
        ]
        result = strip_thinking_blocks(items)
        self.assertEqual(result, [self._msg("assistant", [])])

    def test_keeps_text_alongside_thinking(self) -> None:
        """Text blocks survive when thinking blocks are also present."""
        items = [
            self._msg("assistant", [
                self._text_block("answer"),
                self._thinking_block(),
                self._text_block("more"),
            ]),
        ]
        result = strip_thinking_blocks(items)
        expected = [self._msg("assistant", [
            self._text_block("answer"),
            self._text_block("more"),
        ])]
        self.assertEqual(result, expected)

    def test_does_not_mutate_input(self) -> None:
        """The original list and its dicts are not mutated in place."""
        original = [
            self._msg("assistant", [self._text_block("a"), self._thinking_block()]),
        ]
        items_copy = [dict(m) for m in original]
        result = strip_thinking_blocks(original)
        self.assertEqual(original, items_copy)  # original unchanged
        self.assertNotEqual(result, original)   # result differs


if __name__ == "__main__":
    unittest.main()
