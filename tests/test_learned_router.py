import json
import math
import time
import unittest

from tests.representative_prompts import AGENT_SYSTEM_PROMPT
from zhunt import difficulty
from zhunt.brain import (
    DEFAULT_STRONG_SHARE,
    ClassificationInput,
    HeuristicClassifier,
    LearnedClassifier,
    Tier,
)
from zhunt.engine import classifier_from_env


class FixedModel:
    """Stands in for the trained model so tier logic is tested on its own."""

    def __init__(self, score: float, threshold: float = 0.5) -> None:
        self._score = score
        self._threshold = threshold

    def score(self, text: str) -> float:
        return self._score

    def threshold_for_strong_share(self, share: float) -> float:
        return self._threshold


class ShippedWeightsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = difficulty.default_model()

    def test_weights_load_with_sorted_quantiles_and_metadata(self) -> None:
        self.assertEqual(self.model.size, 1 << 16)
        self.assertEqual(list(self.model.quantiles), sorted(self.model.quantiles))
        self.assertIn("routellm/gpt4_dataset", self.model.meta["source"])
        self.assertGreater(self.model.meta["validation"]["auc"], 0.7)

    def test_scores_are_probabilities_and_deterministic(self) -> None:
        for text in ("hi", "", "x" * 50_000, "数学题：求 x^2 = 4 的解"):
            with self.subTest(text=text[:20]):
                first = self.model.score(text)
                self.assertTrue(0.0 <= first <= 1.0)
                self.assertEqual(first, self.model.score(text))

    def test_hard_prompts_score_above_small_talk(self) -> None:
        easy = self.model.score("Thanks, that worked.")
        hard = self.model.score(
            "Prove that there are infinitely many primes p such that p + 2 is "
            "also prime, or explain precisely why this is still open."
        )
        self.assertGreater(hard, easy)

    def test_threshold_matches_requested_share_on_calibration_scores(self) -> None:
        for share in (0.1, 0.2, 0.5):
            with self.subTest(share=share):
                threshold = self.model.threshold_for_strong_share(share)
                self.assertAlmostEqual(
                    self.model.strong_share_for_threshold(threshold), share, delta=0.02
                )
        self.assertEqual(self.model.threshold_for_strong_share(0.0), math.inf)
        self.assertEqual(self.model.threshold_for_strong_share(1.0), -math.inf)
        with self.assertRaises(ValueError):
            self.model.threshold_for_strong_share(1.5)

    def test_long_prompts_score_quickly(self) -> None:
        text = "Please review this document carefully. " * 5_000
        started = time.perf_counter()
        for _ in range(20):
            self.model.score(text)
        self.assertLess((time.perf_counter() - started) / 20, 0.05)

    def test_weights_round_trip_through_the_file_format(self) -> None:
        weights = [0.0, 1.5, -2.25, 1e-4]
        decoded = difficulty.decode_weights(difficulty.encode_weights(weights))
        for a, b in zip(weights, decoded):
            self.assertAlmostEqual(a, b, places=3)

    def test_truncated_weights_are_rejected(self) -> None:
        blob = difficulty.encode_weights([1.0, 2.0])
        blob["size"] = 3
        with self.assertRaises(ValueError):
            difficulty.decode_weights(blob)

    def test_unsorted_quantiles_are_rejected(self) -> None:
        data = json.loads(difficulty.WEIGHTS_PATH.read_text(encoding="utf-8"))
        data["quantiles"] = [0.9, 0.1]
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                difficulty.load(path)


class LearnedClassifierTests(unittest.TestCase):
    def test_high_difficulty_goes_to_reasoning_with_a_reason(self) -> None:
        result = LearnedClassifier(model=FixedModel(0.9)).classify(
            ClassificationInput(user_text="Compare these two proofs for me")
        )
        self.assertEqual(result.tier, Tier.REASONING)
        self.assertIn("difficulty 0.90", result.reasons[0])

    def test_low_difficulty_keyword_no_longer_forces_reasoning(self) -> None:
        # "think" sent the old rule to reasoning; the learned score decides now.
        result = LearnedClassifier(model=FixedModel(0.1)).classify(
            ClassificationInput(user_text="Think of a name for my cat")
        )
        self.assertEqual(result.tier, Tier.CHAT)
        self.assertEqual(
            HeuristicClassifier()
            .classify(ClassificationInput(user_text="Think of a name for my cat"))
            .tier,
            Tier.REASONING,
        )

    def test_structural_tiers_still_apply_below_the_threshold(self) -> None:
        classifier = LearnedClassifier(model=FixedModel(0.1), long_context_tokens=100)
        self.assertEqual(
            classifier.classify(
                ClassificationInput(user_text="Summarize.", estimated_tokens=100)
            ).tier,
            Tier.LONG_CONTEXT,
        )
        self.assertEqual(
            classifier.classify(
                ClassificationInput(user_text="Fix:\n```py\npass\n```")
            ).tier,
            Tier.CODING,
        )
        self.assertEqual(
            classifier.classify(
                ClassificationInput(user_text="Continue.", has_tool_calls=True)
            ).tier,
            Tier.CODING,
        )

    def test_empty_and_very_short_user_text_is_not_scored(self) -> None:
        classifier = LearnedClassifier(model=FixedModel(0.99))
        for text in ("", "hi", "Thanks.", "Continue.", "Say hi please"):
            with self.subTest(text=text):
                self.assertEqual(
                    classifier.classify(ClassificationInput(user_text=text)).tier,
                    Tier.CHAT,
                )
        self.assertEqual(
            classifier.classify(ClassificationInput(user_text="one two three four")).tier,
            Tier.REASONING,
        )

    def test_agent_system_prompt_is_not_scored(self) -> None:
        classifier = LearnedClassifier()
        for user_text in ("Thanks.", "Continue.", "What changed?"):
            with self.subTest(user_text=user_text):
                result = classifier.classify(
                    ClassificationInput(
                        user_text=user_text,
                        system_prompt=AGENT_SYSTEM_PROMPT,
                        estimated_tokens=2_000,
                    )
                )
                self.assertEqual(result.tier, Tier.CHAT)

    def test_share_zero_never_and_share_one_always_picks_reasoning(self) -> None:
        text = ClassificationInput(user_text="What is 2 + 2?")
        self.assertNotEqual(LearnedClassifier(strong_share=0.0).classify(text).tier, Tier.REASONING)
        self.assertEqual(LearnedClassifier(strong_share=1.0).classify(text).tier, Tier.REASONING)


class ClassifierFromEnvTests(unittest.TestCase):
    def test_learned_router_is_the_default(self) -> None:
        classifier = classifier_from_env({})
        self.assertIsInstance(classifier, LearnedClassifier)
        self.assertEqual(classifier.strong_share, DEFAULT_STRONG_SHARE)

    def test_keyword_rule_can_be_restored(self) -> None:
        classifier = classifier_from_env({"ZHUNT_ROUTER": "keyword"})
        self.assertIs(type(classifier), HeuristicClassifier)

    def test_strong_share_is_read_and_validated(self) -> None:
        self.assertEqual(classifier_from_env({"ZHUNT_STRONG_SHARE": "0.35"}).strong_share, 0.35)
        for bad in ("2", "-0.1", "lots"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                classifier_from_env({"ZHUNT_STRONG_SHARE": bad})


if __name__ == "__main__":
    unittest.main()
