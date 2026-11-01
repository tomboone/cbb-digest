import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine

from cbb_digest import db, liveness
from cbb_digest.scheduler import MAX_SLEEP, Scheduler, next_send_time
from tests.fakes import FakeClock

EASTERN = ZoneInfo("America/New_York")


def et(*args: int) -> datetime:
    return datetime(*args, tzinfo=EASTERN)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(et(2026, 11, 15, 23), et(2026, 11, 16, 7), id="sunday-23:00"),
        pytest.param(et(2026, 11, 16, 6, 59), et(2026, 11, 16, 7), id="monday-06:59"),
        pytest.param(et(2026, 11, 16, 7), et(2026, 11, 23, 7), id="monday-07:00-is-next-week"),
        pytest.param(et(2026, 11, 18, 12), et(2026, 11, 23, 7), id="midweek"),
        # Clocks fall back at 02:00 on Sunday 2026-11-01: EDT before, EST after.
        pytest.param(et(2026, 10, 31, 12), et(2026, 11, 2, 7), id="fall-back-week"),
        pytest.param(et(2026, 11, 1, 3), et(2026, 11, 2, 7), id="just-after-fall-back"),
        # Clocks spring forward at 02:00 on Sunday 2027-03-14: EST before, EDT after.
        pytest.param(et(2027, 3, 13, 12), et(2027, 3, 15, 7), id="spring-forward-week"),
        pytest.param(et(2027, 3, 14, 3), et(2027, 3, 15, 7), id="just-after-spring-forward"),
    ],
)
def test_next_send_time_is_the_next_monday_0700_eastern(now: datetime, expected: datetime) -> None:
    result = next_send_time(now)

    assert result == expected
    local = result.astimezone(EASTERN)
    assert (local.weekday(), local.hour, local.minute) == (0, 7, 0)


def test_next_send_time_tracks_the_utc_offset_across_dst() -> None:
    assert next_send_time(et(2026, 10, 31, 12)).utcoffset() == timedelta(hours=-5)
    assert next_send_time(et(2027, 3, 13, 12)).utcoffset() == timedelta(hours=-4)


def test_next_send_time_accepts_any_time_zone() -> None:
    # Monday 2026-11-16 11:59 UTC is 06:59 EST.
    assert next_send_time(datetime(2026, 11, 16, 11, 59, tzinfo=UTC)) == et(2026, 11, 16, 7)


def test_next_send_time_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        next_send_time(datetime(2026, 11, 16, 6, 59))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    yield engine
    engine.dispose()


def test_a_cycle_writes_the_heartbeat_and_sleeps_at_most_an_hour(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 18, 12))

    Scheduler(clock, engine).cycle()

    assert liveness.last_tick(engine) == et(2026, 11, 18, 12)
    assert clock.sleeps == [et(2026, 11, 18, 12) + MAX_SLEEP]


def test_a_cycle_wakes_at_the_send_time_when_it_is_under_an_hour_away(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 16, 6, 30))

    Scheduler(clock, engine).cycle()

    assert clock.sleeps == [et(2026, 11, 16, 7)]


def test_each_cycle_refreshes_the_heartbeat(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 18, 12))
    scheduler = Scheduler(clock, engine)

    scheduler.cycle()
    scheduler.cycle()

    assert liveness.last_tick(engine) == et(2026, 11, 18, 13)


class StoppingClock(FakeClock):
    """Sets `stop` on its `n`th sleep, as a SIGTERM arriving mid-sleep would."""

    def __init__(self, start: datetime, stop: threading.Event, n: int) -> None:
        super().__init__(start)
        self._stop = stop
        self._n = n

    def sleep_until(self, when: datetime) -> None:
        super().sleep_until(when)
        if len(self.sleeps) == self._n:
            self._stop.set()


def test_run_forever_returns_once_stop_is_set(engine: Engine) -> None:
    stop = threading.Event()
    clock = StoppingClock(et(2026, 11, 18, 12), stop, n=3)

    Scheduler(clock, engine, stop=stop).run_forever()

    assert len(clock.sleeps) == 3
    assert liveness.last_tick(engine) == et(2026, 11, 18, 14)
