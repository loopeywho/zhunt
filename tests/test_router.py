import unittest

from tests.representative_prompts import AGENT_SYSTEM_PROMPT
from zhunt.brain import HeuristicClassifier, Tier
from zhunt.registry import ModelRegistry
from zhunt.router import (
    FailureKind,
    RoutingCoordinator,
    RoutingRequest,
)


REGISTRY = {
    "aliases": {
        "zhunt-auto": {"tier": "auto"},
        "zhunt-chat": {"tier": "chat"},
        "zhunt-coding": {"tier": "coding"},
        "zhunt-reasoning": {"tier": "reasoning"},
    },
    "tiers": {
        "chat": [{"model": "chat-model", "in": 0.1, "out": 0.2}],
        "coding": [{"model": "coding-model", "in": 0.2, "out": 0.4}],
        "long-context": [{"model": "long-model", "in": 0.3, "out": 0.6}],
        "reasoning": [{"model": "reasoning-model", "in": 1, "out": 2}],
    },
}


def request(
    *,
    alias: str = "zhunt-auto",
    text: str = "Hello",
    session_id: str = "session-1",
    tokens: int = 10,
) -> RoutingRequest:
    return RoutingRequest(
        model_alias=alias,
        user_text=text,
        first_user_message=text,
        session_id=session_id,
        estimated_input_tokens=tokens,
        estimated_output_tokens=20,
    )


class RoutingCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(REGISTRY),
            classifier=HeuristicClassifier(long_context_tokens=100),
        )

    def test_auto_alias_classifies_then_selects_model(self) -> None:
        decision = self.coordinator.route(
            request(text="Fix this:\n```python\npass\n```")
        )

        self.assertEqual(decision.tier, Tier.CODING)
        self.assertEqual(decision.model, "coding-model")
        self.assertFalse(decision.explicit_override)
        self.assertIsNotNone(decision.classification)

    def test_explicit_alias_bypasses_classifier(self) -> None:
        decision = self.coordinator.route(
            request(alias="zhunt-chat", text="Architect a distributed system")
        )

        self.assertEqual(decision.tier, Tier.CHAT)
        self.assertEqual(decision.model, "chat-model")
        self.assertTrue(decision.explicit_override)
        self.assertIsNone(decision.classification)

    def test_auto_route_reuses_sticky_session_model(self) -> None:
        first = self.coordinator.route(
            request(text="Fix this:\n```python\npass\n```")
        )
        second = self.coordinator.route(request(text="Thanks"))

        self.assertEqual(second.model, first.model)
        self.assertEqual(second.tier, Tier.CODING)
        self.assertTrue(second.reused_session_route)

    def test_auto_route_promotes_when_classification_rises(self) -> None:
        self.coordinator.route(request(text="Hello"))

        promoted = self.coordinator.route(request(text="Please prove this theorem"))

        self.assertEqual(promoted.tier, Tier.REASONING)
        self.assertEqual(promoted.model, "reasoning-model")
        self.assertFalse(promoted.reused_session_route)

    def test_agent_boilerplate_does_not_pin_session_to_reasoning(self) -> None:
        coordinator = RoutingCoordinator(
            registry=ModelRegistry.from_data(REGISTRY),
            classifier=HeuristicClassifier(),
        )
        initial = coordinator.route(
            RoutingRequest(
                model_alias="zhunt-auto",
                user_text="Fix the failing parser test.",
                first_user_message="Fix the failing parser test.",
                system_prompt=AGENT_SYSTEM_PROMPT,
                session_id="agent-session",
                estimated_input_tokens=2_000,
                estimated_output_tokens=200,
                has_tool_calls=True,
            )
        )
        follow_up = coordinator.route(
            RoutingRequest(
                model_alias="zhunt-auto",
                user_text="The test passes now.",
                first_user_message="Fix the failing parser test.",
                system_prompt=AGENT_SYSTEM_PROMPT,
                session_id="agent-session",
                estimated_input_tokens=2_000,
                estimated_output_tokens=100,
            )
        )

        self.assertEqual(initial.tier, Tier.CODING)
        self.assertEqual(follow_up.tier, Tier.CODING)
        self.assertTrue(follow_up.reused_session_route)

    def test_first_failure_escalates_one_tier(self) -> None:
        initial = self.coordinator.route(request(text="Hello"))

        retry = self.coordinator.escalate(
            initial,
            FailureKind.PROVIDER_ERROR,
        )

        self.assertEqual(retry.tier, Tier.CODING)
        self.assertEqual(retry.model, "coding-model")
        self.assertEqual(retry.escalation_count, 1)
        self.assertEqual(retry.failure, FailureKind.PROVIDER_ERROR)

    def test_second_failure_pins_session_to_top_tier(self) -> None:
        initial = self.coordinator.route(request(text="Hello"))
        first_retry = self.coordinator.escalate(initial, FailureKind.TRUNCATION)

        second_retry = self.coordinator.escalate(
            first_retry,
            FailureKind.REFUSAL,
        )
        later = self.coordinator.route(request(text="Hello again"))

        self.assertEqual(second_retry.tier, Tier.REASONING)
        self.assertEqual(second_retry.model, "reasoning-model")
        self.assertEqual(second_retry.escalation_count, 2)
        self.assertEqual(later.tier, Tier.REASONING)

    def test_escalation_at_top_tier_stays_at_top(self) -> None:
        initial = self.coordinator.route(
            request(alias="zhunt-reasoning", text="Prove it")
        )

        retry = self.coordinator.escalate(initial, FailureKind.REFUSAL)

        self.assertEqual(retry.tier, Tier.REASONING)
        self.assertEqual(retry.model, "reasoning-model")

    def test_health_filter_is_applied_during_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "no healthy models"):
            self.coordinator.route(request(), healthy_models=set())


class TierEscalationTests(unittest.TestCase):
    def test_next_higher_caps_at_highest_tier(self) -> None:
        self.assertEqual(Tier.CHAT.next_higher(), Tier.CODING)
        self.assertEqual(Tier.REASONING.next_higher(), Tier.REASONING)
        self.assertEqual(Tier.highest(), Tier.REASONING)


if __name__ == "__main__":
    unittest.main()
