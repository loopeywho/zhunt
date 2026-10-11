import logging

from tests.catalog_fixtures import FakeClock
from zhunt.catalog import Limits
from zhunt.quota import FailureKind, QuotaTracker
from zhunt.state import StateStore

SLOT = ("alpha/model", "key-1")


def restart(path, clock):
    """A fresh tracker over the same database, as after a daemon restart."""

    return QuotaTracker(clock=clock, store=StateStore.open(path))


def test_daily_usage_survives_a_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    clock = FakeClock()
    quota = restart(path, clock)
    limits = Limits(rpd=2, tpd=1_000)
    lease = quota.try_acquire(SLOT, limits, tokens=100)
    quota.settle(lease, 400)
    assert quota.try_acquire(SLOT, limits, tokens=100)

    clock.advance(3_600)
    again = restart(path, clock)

    assert again.try_acquire(SLOT, limits) is None  # rpd=2 already spent today
    assert again.headroom(SLOT, Limits(tpd=1_000)) == 0.5  # 400 + 100 tokens


def test_minute_window_is_restored_but_only_for_its_last_minute(tmp_path) -> None:
    path = tmp_path / "state.db"
    clock = FakeClock()
    quota = restart(path, clock)
    assert quota.try_acquire(SLOT, Limits(rpm=1))

    clock.advance(30)
    assert restart(path, clock).try_acquire(SLOT, Limits(rpm=1)) is None
    clock.advance(31)
    assert restart(path, clock).try_acquire(SLOT, Limits(rpm=1))


def test_yesterdays_usage_does_not_count_after_a_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    clock = FakeClock(now=10 * 86_400 + 86_000)
    quota = restart(path, clock)
    assert quota.try_acquire(SLOT, Limits(rpd=1))

    clock.advance(1_000)  # past 00:00 UTC

    assert restart(path, clock).try_acquire(SLOT, Limits(rpd=1))


def test_cooldown_and_its_level_survive_a_restart(tmp_path) -> None:
    path = tmp_path / "state.db"
    clock = FakeClock()
    quota = restart(path, clock)
    first = quota.record_failure(SLOT, FailureKind.SERVER)
    clock.advance(first + 1)
    second = quota.record_failure(SLOT, FailureKind.SERVER)
    clock.advance(10)

    again = restart(path, clock)

    assert again.cooldown_remaining(SLOT) == second - 10
    assert again.try_acquire(SLOT, Limits()) is None
    clock.advance(second)
    # The ladder resumes where it left off rather than restarting at step one.
    assert again.record_failure(SLOT, FailureKind.SERVER) == 3_600


def test_rows_older_than_two_days_are_pruned(tmp_path) -> None:
    path = tmp_path / "state.db"
    clock = FakeClock()
    quota = restart(path, clock)
    quota.try_acquire(SLOT, Limits())
    quota.record_failure(SLOT, FailureKind.SERVER)

    clock.advance(3 * 86_400)
    store = StateStore.open(path)
    QuotaTracker(clock=clock, store=store)

    assert store.usage_since(0) == []
    assert store.cooldowns() == []


def test_an_unopenable_database_falls_back_to_memory_with_one_warning(
    tmp_path, caplog
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with caplog.at_level(logging.WARNING):
        store = StateStore.open(blocker / "state.db")

    assert store is None
    assert len(caplog.records) == 1
    quota = QuotaTracker(clock=FakeClock(), store=store)
    assert quota.try_acquire(SLOT, Limits(rpm=1))


def test_a_database_failure_mid_run_disables_persistence_without_crashing(
    tmp_path, caplog
) -> None:
    store = StateStore.open(tmp_path / "state.db")
    quota = QuotaTracker(clock=FakeClock(), store=store)
    store._conn.close()  # simulate the file vanishing under us

    with caplog.at_level(logging.WARNING):
        lease = quota.try_acquire(SLOT, Limits(rpm=2))
        quota.settle(lease, 5)
        quota.record_failure(SLOT, FailureKind.SERVER)

    assert lease is not None
    assert not store.enabled
    assert len(caplog.records) == 1
