import unittest

from zhunt.brain import ClassificationInput, HeuristicClassifier, Tier


class HeuristicClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = HeuristicClassifier(long_context_tokens=100)

    def test_short_conversation_uses_chat_tier(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(user_text="Thanks, that worked.")
        )

        self.assertEqual(result.tier, Tier.CHAT)

    def test_code_fence_uses_coding_tier(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(user_text="Fix this:\n```python\nprint('oops')\n```")
        )

        self.assertEqual(result.tier, Tier.CODING)
        self.assertIn("code fence", result.reasons)

    def test_diff_marker_uses_coding_tier(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(
                user_text="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py"
            )
        )

        self.assertEqual(result.tier, Tier.CODING)

    def test_tool_call_uses_coding_tier(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(user_text="Continue.", has_tool_calls=True)
        )

        self.assertEqual(result.tier, Tier.CODING)

    def test_long_request_uses_long_context_tier(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(user_text="Summarize this.", estimated_tokens=100)
        )

        self.assertEqual(result.tier, Tier.LONG_CONTEXT)

    def test_reasoning_marker_takes_priority(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(
                user_text="Plan the safest fix for this code:\n```python\npass\n```",
                estimated_tokens=1_000,
            )
        )

        self.assertEqual(result.tier, Tier.REASONING)

    def test_system_prompt_can_request_reasoning(self) -> None:
        result = self.classifier.classify(
            ClassificationInput(
                user_text="Go ahead.",
                system_prompt="Reason carefully before answering.",
            )
        )

        self.assertEqual(result.tier, Tier.REASONING)

    def test_invalid_long_context_threshold_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            HeuristicClassifier(long_context_tokens=0)


class TierTests(unittest.TestCase):
    def test_tiers_have_a_monotonic_safety_order(self) -> None:
        self.assertTrue(Tier.CODING.is_higher_than(Tier.CHAT))
        self.assertTrue(Tier.LONG_CONTEXT.is_higher_than(Tier.CODING))
        self.assertTrue(Tier.REASONING.is_higher_than(Tier.LONG_CONTEXT))
        self.assertFalse(Tier.CHAT.is_higher_than(Tier.REASONING))


if __name__ == "__main__":
    unittest.main()
