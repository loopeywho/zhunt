from datetime import datetime, timezone
from threading import Thread

import pytest

from tests.catalog_fixtures import FakeClock
from zhunt.catalog import Limits
from zhunt.quota import FailureKind, QuotaTracker, seconds_until_midnight

SLOT = ("alpha/model", "key-1")
PACIFIC = "America/Los_Angeles"


def test_requests_per_minute_window_slides() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpm=2)

    assert quota.try_acquire(SLOT, limits)
    clock.advance(10)
    assert quota.try_acquire(SLOT, limits)
    assert quota.try_acquire(SLOT, limits) is None

    clock.advance(51)  # first request is now 61s old; second is 51s old
    assert quota.try_acquire(SLOT, limits)
    assert quota.try_acquire(SLOT, limits) is None


def test_denied_acquire_records_nothing() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpm=1)

    assert quota.try_acquire(SLOT, limits)
    for _ in range(5):
        assert quota.try_acquire(SLOT, limits) is None
    clock.advance(61)
    assert quota.try_acquire(SLOT, limits)
    assert quota.try_acquire(SLOT, limits) is None


def test_daily_limit_resets_at_utc_midnight() -> None:
    clock = FakeClock(now=10 * 86_400 + 86_398)
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpd=1)

    assert quota.try_acquire(SLOT, limits)
    clock.advance(1)
    assert quota.try_acquire(SLOT, limits) is None
    clock.advance(2)  # crosses 00:00 UTC
    assert quota.try_acquire(SLOT, limits)


def test_token_windows_count_the_estimate() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)

    assert quota.try_acquire(SLOT, Limits(tpm=100), tokens=60)
    assert quota.try_acquire(SLOT, Limits(tpm=100), tokens=60) is None
    assert quota.try_acquire(SLOT, Limits(tpm=100), tokens=40)

    other = ("alpha/other", "key-1")
    assert quota.try_acquire(other, Limits(tpd=50), tokens=50)
    assert quota.try_acquire(other, Limits(tpd=50), tokens=1) is None


def test_settle_replaces_the_estimate_with_actual_usage() -> None:
    quota = QuotaTracker(clock=FakeClock())
    limits = Limits(tpm=100, tpd=100)

    lease = quota.try_acquire(SLOT, limits, tokens=90)
    assert lease
    assert quota.try_acquire(SLOT, limits, tokens=80) is None
    quota.settle(lease, 10)
    assert quota.try_acquire(SLOT, limits, tokens=80)


def test_cooldown_escalates_across_episodes_and_blocks_until_it_expires() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits()
    durations = []
    for _ in range(6):
        durations.append(quota.record_failure(SLOT, FailureKind.SERVER))
        clock.advance(durations[-1] + 1)  # each failure comes after the last cooldown

    assert durations == [120, 600, 3_600, 86_400, 86_400, 86_400]

    quota.record_success(SLOT)
    quota.record_failure(SLOT, FailureKind.SERVER)
    assert quota.cooldown_remaining(SLOT) == 120
    assert quota.try_acquire(SLOT, limits) is None
    clock.advance(119)
    assert quota.try_acquire(SLOT, limits) is None
    clock.advance(2)
    assert quota.try_acquire(SLOT, limits)


def test_a_success_after_the_cooldown_clears_the_failure_streak() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    quota.record_failure(SLOT, FailureKind.SERVER)
    clock.advance(121)
    quota.record_failure(SLOT, FailureKind.SERVER)  # second episode: 600 s
    clock.advance(601)

    quota.record_success(SLOT)

    assert quota.cooldown_remaining(SLOT) == 0
    assert quota.record_failure(SLOT, FailureKind.SERVER) == 120


@pytest.mark.parametrize(
    ("kind", "each"), [(FailureKind.SERVER, 120), (FailureKind.RATE_LIMIT, 90)]
)
def test_parallel_failures_in_one_burst_count_as_one_episode(kind, each) -> None:
    quota = QuotaTracker(clock=FakeClock())

    burst = [quota.record_failure(SLOT, kind) for _ in range(3)]

    assert burst == [each] * 3  # not 120 -> 600 -> 3600


def test_a_later_short_failure_never_shortens_a_longer_cooldown() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.CREDIT)  # a day

    quota.record_failure(SLOT, FailureKind.RATE_LIMIT, retry_after=5)

    assert quota.cooldown_remaining(SLOT) == 86_400


def test_an_in_flight_success_does_not_lift_a_fresh_cooldown() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.SERVER)

    quota.record_success(SLOT)  # an earlier request finishing late

    assert quota.cooldown_remaining(SLOT) == 120


def test_provider_retry_after_is_a_floor_capped_at_a_day() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpd=1_000)

    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT, limits=limits, retry_after=17) == 90
    clock.advance(91)
    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT, limits=limits, retry_after=500) == 500
    clock.advance(501)
    assert quota.record_failure(SLOT, FailureKind.SERVER, retry_after=10**7) == 86_400


def test_a_per_minute_429_gets_90_seconds_and_never_climbs_the_ladder() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpm=10, rpd=1_000)  # the daily cap is far from spent

    for _ in range(4):
        assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT, limits=limits) == 90
        clock.advance(91)

    assert quota.record_failure(SLOT, FailureKind.SERVER) == 120  # ladder untouched


def test_a_429_with_the_daily_counter_at_its_cap_climbs_the_ladder() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpd=1)
    assert quota.try_acquire(SLOT, limits)

    first = quota.record_failure(SLOT, FailureKind.RATE_LIMIT, limits=limits)
    clock.advance(first + 1)
    second = quota.record_failure(SLOT, FailureKind.RATE_LIMIT, limits=limits)

    assert (first, second) == (120, 600)


def test_repeated_429s_with_no_daily_cap_escalate_but_only_to_ten_minutes() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    durations = []
    for _ in range(5):
        durations.append(quota.record_failure(SLOT, FailureKind.RATE_LIMIT))
        clock.advance(durations[-1] + 1)

    assert durations == [90, 120, 600, 600, 600]


def test_429s_more_than_an_hour_apart_stay_transient() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)

    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT) == 90
    clock.advance(3_700)
    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT) == 90


def test_a_spent_daily_quota_benches_until_utc_midnight() -> None:
    clock = FakeClock(now=10 * 86_400 + 80_000)
    quota = QuotaTracker(clock=clock)

    assert quota.record_failure(SLOT, FailureKind.DAILY_QUOTA) == 6_400


def test_a_rolling_daily_window_trusts_the_providers_retry_after() -> None:
    quota = QuotaTracker(clock=FakeClock(now=10 * 86_400 + 10_000))

    assert quota.record_failure(SLOT, FailureKind.DAILY_QUOTA, retry_after=432) == 432


def test_google_daily_quota_resets_at_pacific_midnight() -> None:
    # 2026-07-01 12:00 UTC is 05:00 PDT; midnight Pacific is 07:00 UTC next day.
    noon = datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp()
    quota = QuotaTracker(clock=FakeClock(now=noon))

    benched = quota.record_failure(
        SLOT, FailureKind.DAILY_QUOTA, retry_after=30, daily_reset_zone=PACIFIC
    )

    assert benched == 19 * 3_600  # a short retry-after cannot reopen it early


@pytest.mark.parametrize(
    ("utc", "hours"),
    [
        (datetime(2026, 3, 7, 20, tzinfo=timezone.utc), 12),  # PST, the day before DST
        (datetime(2026, 3, 8, 19, tzinfo=timezone.utc), 12),  # PDT, the DST day itself
        (datetime(2026, 11, 1, 19, tzinfo=timezone.utc), 13),  # back to PST at 08:00 UTC
    ],
)
def test_seconds_until_pacific_midnight_follow_daylight_saving(utc, hours) -> None:
    assert seconds_until_midnight(utc.timestamp(), PACIFIC) == hours * 3_600


@pytest.mark.parametrize(
    ("kind", "seconds"),
    [
        (FailureKind.AUTH, 300),
        (FailureKind.CREDIT, 86_400),
        (FailureKind.FORBIDDEN, 86_400),
        (FailureKind.NOT_FOUND, 86_400),
    ],
)
def test_key_and_model_failures_get_fixed_benches(kind: FailureKind, seconds) -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)

    first = quota.record_failure(SLOT, kind)
    clock.advance(first + 1)

    assert first == seconds
    assert quota.record_failure(SLOT, kind) == seconds  # no ladder


def test_three_model_failures_in_fifteen_minutes_bench_it_on_every_key() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    slots = [("alpha/model", "key-1"), ("alpha/model", "key-2")]

    assert not quota.record_model_failure("alpha/model", slots)
    clock.advance(300)
    assert not quota.record_model_failure("alpha/model", slots)
    clock.advance(300)
    assert quota.record_model_failure("alpha/model", slots)

    assert [quota.cooldown_remaining(s) for s in slots] == [600, 600]


def test_model_failure_streak_window_slides_and_a_success_clears_it() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    slots = [SLOT]

    quota.record_model_failure("alpha/model", slots)
    clock.advance(901)  # the first failure has left the 15-minute window
    quota.record_model_failure("alpha/model", slots)
    quota.record_success(SLOT)
    quota.record_model_failure("alpha/model", slots)

    assert quota.cooldown_remaining(SLOT) == 0


def test_a_model_streak_never_shortens_a_longer_bench() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.FORBIDDEN)
    for _ in range(3):
        quota.record_model_failure("alpha/model", [SLOT])

    assert quota.cooldown_remaining(SLOT) == 86_400


def test_failures_are_tracked_per_slot() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.SERVER)

    assert quota.cooldown_remaining(("alpha/model", "key-2")) == 0
    assert quota.try_acquire(("alpha/model", "key-2"), Limits())


def test_a_learned_limit_fills_an_unknown_cap_and_is_enforced() -> None:
    quota = QuotaTracker(clock=FakeClock())

    assert quota.learn_limit("alpha/model", Limits(), "rpd", 1)
    assert quota.try_acquire(SLOT, Limits())
    assert quota.try_acquire(SLOT, Limits()) is None
    assert quota.headroom(SLOT, Limits()) == 0.0
    assert quota.effective_limits("alpha/model", Limits(rpm=5)) == Limits(rpm=5, rpd=1)


def test_a_learned_limit_only_ever_lowers_a_cap() -> None:
    quota = QuotaTracker(clock=FakeClock())
    limits = Limits(tpm=10_000)

    assert not quota.learn_limit("alpha/model", limits, "tpm", 30_000)
    assert quota.learn_limit("alpha/model", limits, "tpm", 6_000)
    assert not quota.learn_limit("alpha/model", limits, "tpm", 8_000)
    assert not quota.learn_limit("alpha/model", limits, "bogus", 1)
    assert quota.effective_limits("alpha/model", limits).tpm == 6_000
    assert quota.effective_limits("alpha/other", limits).tpm == 10_000


def test_a_429_at_a_learned_daily_cap_climbs_the_ladder() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.learn_limit("alpha/model", Limits(), "rpd", 1)
    assert quota.try_acquire(SLOT, Limits())

    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT) == 120


def test_headroom_is_the_tightest_remaining_fraction() -> None:
    quota = QuotaTracker(clock=FakeClock())
    limits = Limits(rpm=4, rpd=100)

    assert quota.headroom(SLOT, limits) == 1.0
    quota.try_acquire(SLOT, limits)
    assert quota.headroom(SLOT, limits) == 0.75
    assert quota.headroom(SLOT, Limits()) == 1.0


def test_concurrent_acquires_never_exceed_the_limit() -> None:
    quota = QuotaTracker(clock=FakeClock())
    limits = Limits(rpm=10)
    results: list[bool] = []

    def worker() -> None:
        results.append(quota.try_acquire(SLOT, limits) is not None)

    threads = [Thread(target=worker) for _ in range(60)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 10


def test_usage_reports_each_window_beside_its_cap() -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)
    limits = Limits(rpm=30, rpd=1000, tpd=None)

    lease = quota.try_acquire(SLOT, limits, tokens=500)
    quota.settle(lease, 320)
    quota.try_acquire(SLOT, limits, tokens=100)

    usage = quota.usage(SLOT, limits)
    assert usage["rpm"] == {"used": 2, "cap": 30}
    assert usage["rpd"] == {"used": 2, "cap": 1000}
    assert usage["tpm"] == {"used": 420, "cap": None}
    assert usage["tpd"] == {"used": 420, "cap": None}
    clock.advance(61)
    assert quota.usage(SLOT, limits)["rpm"]["used"] == 0
