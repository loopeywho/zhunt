from threading import Thread

import pytest

from tests.catalog_fixtures import FakeClock
from zhunt.catalog import Limits
from zhunt.quota import FailureKind, QuotaTracker

SLOT = ("alpha/model", "key-1")


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
        durations.append(quota.record_failure(SLOT, FailureKind.RATE_LIMIT))
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


def test_parallel_failures_in_one_burst_count_as_one_episode() -> None:
    quota = QuotaTracker(clock=FakeClock())

    burst = [quota.record_failure(SLOT, FailureKind.RATE_LIMIT) for _ in range(3)]

    assert burst == [120, 120, 120]  # not 120 -> 600 -> 3600


def test_a_later_short_failure_never_shortens_a_longer_cooldown() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.CREDIT)  # 3600 s

    quota.record_failure(SLOT, FailureKind.RATE_LIMIT, retry_after=5)

    assert quota.cooldown_remaining(SLOT) == 3_600


def test_an_in_flight_success_does_not_lift_a_fresh_cooldown() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.SERVER)

    quota.record_success(SLOT)  # an earlier request finishing late

    assert quota.cooldown_remaining(SLOT) == 120


def test_provider_retry_after_is_authoritative() -> None:
    quota = QuotaTracker(clock=FakeClock())

    assert quota.record_failure(SLOT, FailureKind.RATE_LIMIT, retry_after=17) == 17
    assert quota.cooldown_remaining(SLOT) == 17


@pytest.mark.parametrize("kind", [FailureKind.AUTH, FailureKind.CREDIT])
def test_auth_and_credit_failures_start_at_one_hour(kind: FailureKind) -> None:
    clock = FakeClock()
    quota = QuotaTracker(clock=clock)

    first = quota.record_failure(SLOT, kind)
    clock.advance(first + 1)

    assert first == 3_600
    assert quota.record_failure(SLOT, kind) == 86_400


def test_failures_are_tracked_per_slot() -> None:
    quota = QuotaTracker(clock=FakeClock())
    quota.record_failure(SLOT, FailureKind.SERVER)

    assert quota.cooldown_remaining(("alpha/model", "key-2")) == 0
    assert quota.try_acquire(("alpha/model", "key-2"), Limits())


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
