import unittest

from zhunt.brain import SessionStickiness, Tier, session_key


MODELS = {
    Tier.CHAT: "chat-model",
    Tier.CODING: "coding-model",
    Tier.LONG_CONTEXT: "long-model",
    Tier.REASONING: "reasoning-model",
}


class SessionStickinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stickiness = SessionStickiness()

    def test_first_request_selects_and_stores_model(self) -> None:
        decision = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CODING,
            select_model=MODELS.__getitem__,
        )

        self.assertTrue(decision.changed)
        self.assertEqual(decision.route.model, "coding-model")
        self.assertEqual(self.stickiness.get("session-1"), decision.route)

    def test_session_never_downgrades(self) -> None:
        self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.REASONING,
            select_model=MODELS.__getitem__,
        )

        decision = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=MODELS.__getitem__,
        )

        self.assertFalse(decision.changed)
        self.assertEqual(decision.route.tier, Tier.REASONING)
        self.assertEqual(decision.route.model, "reasoning-model")

    def test_session_moves_when_tier_rises(self) -> None:
        self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=MODELS.__getitem__,
        )

        decision = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CODING,
            select_model=MODELS.__getitem__,
        )

        self.assertTrue(decision.changed)
        self.assertEqual(decision.route.model, "coding-model")

    def test_equal_tier_does_not_reselect_model(self) -> None:
        selections = 0

        def select_model(tier: Tier) -> str:
            nonlocal selections
            selections += 1
            return f"model-{selections}"

        first = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=select_model,
        )
        second = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=select_model,
        )

        self.assertEqual(selections, 1)
        self.assertEqual(second.route, first.route)

    def test_sessions_are_independent(self) -> None:
        first = self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=MODELS.__getitem__,
        )
        second = self.stickiness.choose(
            session_key="session-2",
            classified_tier=Tier.CODING,
            select_model=MODELS.__getitem__,
        )

        self.assertNotEqual(first.route, second.route)

    def test_clear_forgets_route(self) -> None:
        self.stickiness.choose(
            session_key="session-1",
            classified_tier=Tier.CHAT,
            select_model=MODELS.__getitem__,
        )

        self.stickiness.clear("session-1")

        self.assertIsNone(self.stickiness.get("session-1"))

    def test_explicit_session_id_is_preferred(self) -> None:
        key = session_key(
            session_id="conversation-123",
            system_prompt="system",
            first_user_message="hello",
        )

        self.assertEqual(key, "id:conversation-123")

    def test_fallback_session_key_is_stable_and_input_sensitive(self) -> None:
        first = session_key(
            session_id=None,
            system_prompt="system",
            first_user_message="hello",
        )
        same = session_key(
            session_id=None,
            system_prompt="system",
            first_user_message="hello",
        )
        different = session_key(
            session_id=None,
            system_prompt="system",
            first_user_message="goodbye",
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, different)
        self.assertTrue(first.startswith("fallback:"))

    def test_empty_session_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.stickiness.choose(
                session_key="",
                classified_tier=Tier.CHAT,
                select_model=MODELS.__getitem__,
            )


if __name__ == "__main__":
    unittest.main()
