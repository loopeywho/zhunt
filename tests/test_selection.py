import random

import pytest

from tests.catalog_fixtures import FakeClock, catalog_data, model, provider
from zhunt.brain import Tier
from zhunt.catalog import Catalog, Limits
from zhunt.quota import FailureKind, QuotaTracker
from zhunt.selection import (
    NoCandidateError,
    Penalties,
    Reliability,
    SelectionPolicy,
    Selector,
    headroom_factor,
)

KEYS = {"alpha": ["a1"], "beta": ["b1"], "gamma": ["g1"], "delta": ["d1"]}


def build(*, policy=SelectionPolicy(), keys=KEYS, data=None, clock=None):
    clock = clock or FakeClock()
    quota = QuotaTracker(clock=clock)
    reliability = Reliability(clock=clock)
    selector = Selector(
        Catalog.from_data(data or catalog_data()),
        quota,
        reliability,
        keys,
        policy=policy,
    )
    return selector, quota, reliability, clock


def ids(candidates) -> list[str]:
    return [candidate.model.id for candidate in candidates]


def test_free_models_rank_ahead_of_paid_ones() -> None:
    selector, *_ = build(policy=SelectionPolicy(allow_paid=True))

    ranked = ids(selector.ranked(Tier.CHAT))

    assert ranked[-1] == "gamma/paid-chat"
    assert set(ranked[:-1]) == {"alpha/chat-fast", "alpha/coder"}


def test_paid_models_are_excluded_by_default() -> None:
    selector, *_ = build()

    assert "gamma/paid-chat" not in ids(selector.ranked(Tier.CHAT))


def test_paid_model_serves_only_after_free_capacity_is_gone() -> None:
    selector, *_ = build(policy=SelectionPolicy(allow_paid=True))
    served = [selector.acquire(Tier.CODING)[0].model.id for _ in range(5)]

    assert served[:3] == ["alpha/coder"] * 3  # rpd=3
    assert served[3:] == ["gamma/paid-chat"] * 2


def test_prompt_training_providers_need_explicit_opt_in() -> None:
    default, *_ = build()
    opted_in, *_ = build(policy=SelectionPolicy(allow_training=True))

    assert "beta/trainer" not in ids(default.ranked(Tier.CHAT))
    assert "beta/trainer" in ids(opted_in.ranked(Tier.CHAT))


def test_tool_requests_skip_models_without_tools() -> None:
    selector, *_ = build()

    assert ids(selector.ranked(Tier.CHAT, requires_tools=True)) == ["alpha/coder"]


def test_models_too_small_for_the_request_are_skipped() -> None:
    data = catalog_data()
    data["providers"]["alpha"]["models"][0]["context_window"] = 1_000
    selector, *_ = build(data=data)

    assert "alpha/chat-fast" not in ids(selector.ranked(Tier.CHAT, tokens=5_000))
    assert "alpha/chat-fast" in ids(selector.ranked(Tier.CHAT, tokens=500))


def test_exhausted_tier_degrades_to_the_next_tier_down() -> None:
    selector, *_ = build()
    first, _ = selector.acquire(Tier.REASONING)
    second, _ = selector.acquire(Tier.REASONING)
    third, _ = selector.acquire(Tier.REASONING)

    assert (first.model.id, first.degraded) == ("delta/thinker", False)
    assert (second.model.id, second.degraded) == ("delta/thinker", False)
    assert third.degraded and third.tier is Tier.CODING


def test_raises_when_every_allowed_model_is_out_of_quota() -> None:
    data = catalog_data()
    data["providers"]["alpha"]["models"][0]["limits"] = {"rpd": 1}
    data["providers"]["alpha"]["models"][1]["limits"] = {"rpd": 1}
    selector, *_ = build(data=data)
    selector.acquire(Tier.CHAT)
    selector.acquire(Tier.CHAT)

    with pytest.raises(NoCandidateError):
        selector.acquire(Tier.CHAT)


def test_benched_models_are_skipped_until_cooldown_ends() -> None:
    selector, quota, _, clock = build()
    quota.record_failure(("alpha/coder", "a1"), FailureKind.RATE_LIMIT)

    assert "alpha/coder" not in ids(selector.ranked(Tier.CHAT))
    clock.advance(121)
    assert "alpha/coder" in ids(selector.ranked(Tier.CHAT))


def test_observed_failures_lower_a_models_rank() -> None:
    selector, _, reliability, _ = build()
    assert ids(selector.ranked(Tier.CHAT))[0] == "alpha/coder"

    for _ in range(10):
        reliability.record("alpha/coder", success=False)

    assert ids(selector.ranked(Tier.CHAT))[0] == "alpha/chat-fast"


def test_old_failures_fade_with_a_two_day_half_life() -> None:
    clock = FakeClock()
    reliability = Reliability(clock=clock)
    for _ in range(10):
        reliability.record("m", success=False)
    fresh = reliability.score("m")

    clock.advance(10 * 86_400)

    assert reliability.score("m") > fresh
    # 10 failures decayed over 5 half-lives leave 10 / 32 of a failure.
    assert reliability.score("m") == pytest.approx(1 / (2 + 10 * 0.5**5))


def test_thompson_sampling_is_seedable_and_bounded() -> None:
    clock = FakeClock()
    a = Reliability(clock=clock, rng=random.Random(7))
    b = Reliability(clock=clock, rng=random.Random(7))
    for tracker in (a, b):
        tracker.record("m", success=True)

    assert a.score("m") == b.score("m")
    assert 0 < a.score("m") < 1


def test_a_second_key_takes_over_when_the_first_is_limited() -> None:
    data = catalog_data()
    data["providers"]["alpha"]["models"][0]["limits"] = {"rpm": 1}
    selector, *_ = build(keys={**KEYS, "alpha": ["a1", "a2"]}, data=data)

    only_chat_fast = frozenset({"alpha/coder"})
    served = [
        (c.model.id, c.key_id)
        for c, _ in (
            selector.acquire(Tier.CHAT, tokens=1, exclude=only_chat_fast)
            for _ in range(2)
        )
    ]

    assert sorted(served) == [("alpha/chat-fast", "a1"), ("alpha/chat-fast", "a2")]


@pytest.mark.parametrize(
    ("remaining", "factor"),
    [(1.0, 1.0), (0.5, 1.0), (0.2, 1.0), (0.1, 0.55), (0.0, 0.1), (-1.0, 0.1)],
)
def test_headroom_only_demotes_a_model_in_its_last_fifth(remaining, factor) -> None:
    assert headroom_factor(remaining) == pytest.approx(factor)


def test_a_half_used_model_keeps_its_full_score() -> None:
    selector, quota, *_ = build()
    before = selector.ranked(Tier.REASONING)[0].score
    quota.try_acquire(("delta/thinker", "d1"), Limits(rpd=2))  # rpd 2: half used

    assert selector.ranked(Tier.REASONING)[0].score == pytest.approx(before)


def test_penalty_adds_three_per_429_one_per_failure_and_caps_at_ten() -> None:
    penalties = Penalties(clock=FakeClock())

    penalties.record_failure("m", rate_limited=True)
    penalties.record_failure("m", rate_limited=False)
    assert penalties.penalty("m") == 4
    assert penalties.factor("m") == pytest.approx(1 - 0.4 * 0.6)

    for _ in range(5):
        penalties.record_failure("m", rate_limited=True)
    assert penalties.penalty("m") == 10
    assert penalties.factor("m") == pytest.approx(0.4)


def test_penalty_decays_one_point_every_two_minutes_and_on_success() -> None:
    clock = FakeClock()
    penalties = Penalties(clock=clock)
    penalties.record_failure("m", rate_limited=True)
    penalties.record_failure("m", rate_limited=True)  # 6

    clock.advance(239)
    assert penalties.penalty("m") == 5
    clock.advance(1)
    assert penalties.penalty("m") == 4
    penalties.record_success("m")
    assert penalties.penalty("m") == 3
    clock.advance(6 * 120)
    assert penalties.penalty("m") == 0
    assert penalties.factor("m") == 1.0


def test_a_rate_limited_model_sinks_below_a_peer_then_recovers() -> None:
    clock = FakeClock()
    penalties = Penalties(clock=clock)
    quota = QuotaTracker(clock=clock)
    selector = Selector(
        Catalog.from_data(catalog_data()),
        quota,
        Reliability(clock=clock),
        KEYS,
        penalties=penalties,
    )
    assert ids(selector.ranked(Tier.CHAT))[0] == "alpha/coder"

    for _ in range(3):
        penalties.record_failure("alpha/coder", rate_limited=True)
    assert ids(selector.ranked(Tier.CHAT))[0] == "alpha/chat-fast"

    clock.advance(20 * 60)
    assert ids(selector.ranked(Tier.CHAT))[0] == "alpha/coder"


def test_invalid_weights_are_rejected() -> None:
    with pytest.raises(ValueError):
        Selector(
            Catalog.from_data(catalog_data()),
            QuotaTracker(),
            Reliability(),
            KEYS,
            weights=(0, 0, 0),
        )


def test_429_storm_still_answers_every_request() -> None:
    """Milestone 1 check: one model always rate-limits, one runs dry mid-run, and
    a third is steady. Every request must still be answered."""

    data = {
        "providers": {
            "storm": provider([model("always-429", ["chat"], intelligence=99, speed=99)]),
            "limited": provider(
                [model("daily-10", ["chat"], limits={"rpd": 10}, intelligence=80, speed=80)]
            ),
            "steady": provider([model("steady", ["chat"], intelligence=20, speed=20)]),
        }
    }
    keys = {"storm": ["s"], "limited": ["l"], "steady": ["t"]}
    selector, quota, reliability, clock = build(data=data, keys=keys)

    answered: dict[str, int] = {}
    attempts_per_request = []
    storm_hits = 0
    for _ in range(200):
        attempts = 0
        while True:
            attempts += 1
            candidate, lease = selector.acquire(Tier.CHAT)
            if candidate.model.id == "storm/always-429":
                storm_hits += 1
                quota.record_failure(candidate.slot, FailureKind.RATE_LIMIT)
                reliability.record(candidate.model.id, success=False)
                continue
            quota.record_success(candidate.slot)
            reliability.record(candidate.model.id, success=True)
            answered[candidate.model.id] = answered.get(candidate.model.id, 0) + 1
            break
        attempts_per_request.append(attempts)
        clock.advance(0.1)

    assert sum(answered.values()) == 200
    assert storm_hits == 1  # benched after the first 429, never retried in 20s
    assert max(attempts_per_request) <= 2
    # Headroom steering shifts traffic to the steady model as the daily cap
    # nears, so the limited model is used but never past its hard cap.
    assert 0 < answered["limited/daily-10"] <= 10
    assert answered["limited/daily-10"] + answered["steady/steady"] == 200
