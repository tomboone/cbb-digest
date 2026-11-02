import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from respx.models import Call
from sqlalchemy import Engine, select

from cbb_digest import db, liveness
from cbb_digest.clock import Clock
from cbb_digest.heartbeat import Heartbeat
from cbb_digest.runs import SENT_STATUSES, FinalStatus, finish_run, start_run
from cbb_digest.scheduler import MAX_SLEEP, Scheduler, next_send_time
from cbb_digest.schema import runs
from cbb_digest.weeks import run_id_for, windows_for
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


SEASON = 2027
# Monday 2026-11-16, run 2027-W47.
MONDAY = date(2026, 11, 16)
RUN_ID = run_id_for(MONDAY, SEASON)


class FakeRun:
    """Stands in for `run`: records each Monday it is called for, at what time, and then
    either raises `error` or records the week with `status`, as the real `run` would."""

    def __init__(
        self,
        clock: Clock,
        engine: Engine,
        *,
        status: FinalStatus = "sent",
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[date, datetime]] = []
        self._clock = clock
        self._engine = engine
        self._status: FinalStatus = status
        self._error = error

    def __call__(self, monday: date) -> None:
        self.calls.append((monday, self._clock.now()))
        if self._error is not None:
            raise self._error
        _record(self._engine, self._status, at=self._clock.now())


def _record(
    engine: Engine, status: FinalStatus | None, *, at: datetime, transport: str = "resend"
) -> None:
    """Give `RUN_ID` a row ending in `status`, or left `running` when it is `None`."""
    with engine.begin() as connection:
        start_run(
            connection,
            run_id=RUN_ID,
            kind="digest",
            season=SEASON,
            windows=windows_for(MONDAY),
            image_sha=None,
            now=at,
        )
        if status is not None:
            sent = status in SENT_STATUSES
            finish_run(
                connection,
                run_id=RUN_ID,
                status=status,
                row_counts={},
                failed_sources=[],
                alerts=[],
                transport=transport if sent else None,
                now=at,
            )


def _status(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            select(runs.c.status).where(runs.c.run_id == RUN_ID)
        ).scalar_one_or_none()


def _scheduler(
    clock: Clock,
    engine: Engine,
    *,
    run_week: FakeRun | None = None,
    dry_run: bool = False,
    heartbeat: Heartbeat | None = None,
    stop: threading.Event | None = None,
) -> Scheduler:
    return Scheduler(
        clock,
        engine,
        season=SEASON,
        run_week=run_week or FakeRun(clock, engine),
        dry_run=dry_run,
        heartbeat=heartbeat,
        stop=stop,
    )


def test_a_cycle_writes_the_heartbeat_and_sleeps_at_most_an_hour(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 18, 12))

    _scheduler(clock, engine).cycle()

    assert liveness.last_tick(engine) == et(2026, 11, 18, 12)
    assert clock.sleeps == [et(2026, 11, 18, 12) + MAX_SLEEP]


def test_a_cycle_wakes_at_the_send_time_when_it_is_under_an_hour_away(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 16, 6, 30))

    _scheduler(clock, engine).cycle()

    assert clock.sleeps == [et(2026, 11, 16, 7)]


def test_each_cycle_refreshes_the_heartbeat(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 18, 12))
    scheduler = _scheduler(clock, engine)

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

    _scheduler(clock, engine, stop=stop).run_forever()

    assert len(clock.sleeps) == 3
    assert liveness.last_tick(engine) == et(2026, 11, 18, 14)


def test_a_boot_at_monday_0600_sleeps_until_0700_then_runs_once(engine: Engine) -> None:
    stop = threading.Event()
    clock = StoppingClock(et(2026, 11, 16, 6), stop, n=4)
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run, stop=stop).run_forever()

    assert clock.sleeps[0] == et(2026, 11, 16, 7)
    assert fake_run.calls == [(MONDAY, et(2026, 11, 16, 7))]
    assert _status(engine) == "sent"


def test_a_boot_at_monday_0900_with_the_week_unsent_runs_at_once(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == [(MONDAY, et(2026, 11, 16, 9))]
    assert clock.sleeps == [et(2026, 11, 16, 10)]


@pytest.mark.parametrize("status", ["sent", "sent_degraded", "skipped_offseason"])
def test_a_boot_at_monday_0900_with_the_week_settled_does_not_run(
    engine: Engine, status: FinalStatus
) -> None:
    _record(engine, status, at=et(2026, 11, 16, 7, 5))
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == []
    assert _status(engine) == status


def test_a_boot_on_a_week_that_failed_runs_it_again(engine: Engine) -> None:
    # A restart is the retry: a fix redeployed on Monday morning still makes that week's send.
    _record(engine, "failed", at=et(2026, 11, 16, 7, 5))
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert len(fake_run.calls) == 1
    assert _status(engine) == "sent"


def test_a_boot_at_tuesday_1159_still_catches_the_week_up(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 17, 11, 59))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == [(MONDAY, et(2026, 11, 17, 11, 59))]


@pytest.mark.parametrize(
    "now",
    [
        pytest.param(et(2026, 11, 17, 12), id="tuesday-12:00"),
        pytest.param(et(2026, 11, 17, 13), id="tuesday-13:00"),
        pytest.param(et(2026, 11, 22, 23), id="sunday-23:00"),
    ],
)
def test_a_boot_after_tuesday_noon_with_the_week_unsent_records_it_missed(
    engine: Engine, now: datetime
) -> None:
    clock = FakeClock(now)
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == []
    assert _status(engine) == "skipped_missed"


def test_a_week_left_running_after_tuesday_noon_is_recorded_missed(engine: Engine) -> None:
    # A `running` row is not an outcome: `load` run alone, or a run killed mid-way.
    _record(engine, None, at=et(2026, 11, 16, 7, 5))

    _scheduler(FakeClock(et(2026, 11, 17, 13)), engine).cycle()

    assert _status(engine) == "skipped_missed"


@pytest.mark.parametrize("status", ["sent", "sent_degraded", "skipped_offseason", "failed"])
def test_a_week_with_an_outcome_is_left_alone_after_tuesday_noon(
    engine: Engine, status: FinalStatus
) -> None:
    _record(engine, status, at=et(2026, 11, 16, 7, 5))

    _scheduler(FakeClock(et(2026, 11, 17, 13)), engine).cycle()

    assert _status(engine) == status


def test_a_week_emailed_but_left_running_is_not_recorded_missed(engine: Engine) -> None:
    # The email went out and the run died before finishing: it was not missed.
    _record(engine, "sent", at=et(2026, 11, 16, 7, 5))
    _record(engine, None, at=et(2026, 11, 16, 8))

    _scheduler(FakeClock(et(2026, 11, 17, 13)), engine).cycle()

    assert _status(engine) == "running"


def test_a_run_started_by_hand_after_tuesday_noon_is_left_running(engine: Engine) -> None:
    _record(engine, None, at=et(2026, 11, 17, 14))

    _scheduler(FakeClock(et(2026, 11, 17, 15)), engine).cycle()

    assert _status(engine) == "running"


def test_monday_before_0700_leaves_the_week_alone(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 16, 6, 59))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == []
    assert _status(engine) is None


def test_an_exception_in_run_leaves_serve_alive_and_records_failed(engine: Engine) -> None:
    stop = threading.Event()
    clock = StoppingClock(et(2026, 11, 16, 9), stop, n=3)
    fake_run = FakeRun(clock, engine, error=RuntimeError("boom"))

    _scheduler(clock, engine, run_week=fake_run, stop=stop).run_forever()

    assert _status(engine) == "failed"
    # Still looping: ticking, sleeping, and trying the week again once each cycle.
    assert len(clock.sleeps) == 3
    assert liveness.last_tick(engine) == et(2026, 11, 16, 11)
    assert [at for _, at in fake_run.calls] == [
        et(2026, 11, 16, 9),
        et(2026, 11, 16, 10),
        et(2026, 11, 16, 11),
    ]


def test_a_failed_week_is_retried_hourly_until_it_sends(engine: Engine) -> None:
    stop = threading.Event()
    clock = StoppingClock(et(2026, 11, 16, 9), stop, n=4)
    failing = FakeRun(clock, engine, error=RuntimeError("Resend down"))
    working = FakeRun(clock, engine)

    def run_week(monday: date) -> None:
        (failing if not failing.calls else working)(monday)

    scheduler = Scheduler(clock, engine, season=SEASON, run_week=run_week, stop=stop)
    scheduler.run_forever()

    assert len(failing.calls) == 1
    assert working.calls == [(MONDAY, et(2026, 11, 16, 10))]
    assert _status(engine) == "sent"


def test_a_failed_week_is_not_retried_after_tuesday_noon(engine: Engine) -> None:
    _record(engine, "failed", at=et(2026, 11, 17, 11))
    clock = FakeClock(et(2026, 11, 17, 12))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert fake_run.calls == []
    assert _status(engine) == "failed"


def test_a_failed_run_that_recorded_itself_keeps_its_own_row(engine: Engine) -> None:
    clock = FakeClock(et(2026, 11, 16, 9))

    class RecordsThenRaises(FakeRun):
        def __call__(self, monday: date) -> None:
            _record(engine, "failed", at=et(2026, 11, 16, 8))
            raise RuntimeError("boom")

    _scheduler(clock, engine, run_week=RecordsThenRaises(clock, engine)).cycle()

    with engine.connect() as connection:
        finished_at = connection.execute(
            select(runs.c.finished_at).where(runs.c.run_id == RUN_ID)
        ).scalar_one()
    assert finished_at == et(2026, 11, 16, 8)


def test_a_dry_run_week_does_not_block_the_real_send(engine: Engine) -> None:
    # Spec §9.2: an outbox send emailed nobody, so the real serve still sends the week.
    _record(engine, "sent", at=et(2026, 11, 16, 7, 5), transport="outbox")
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run).cycle()

    assert len(fake_run.calls) == 1


def test_a_dry_run_serve_does_not_repeat_a_week_it_dry_ran(engine: Engine) -> None:
    _record(engine, "sent", at=et(2026, 11, 16, 7, 5), transport="outbox")
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine)

    _scheduler(clock, engine, run_week=fake_run, dry_run=True).cycle()

    assert fake_run.calls == []


# --- healthchecks: the outcomes `run` cannot ping itself ---

HC_URL = "https://hc-ping.com/abc-123"


@pytest.fixture
def pings() -> Iterator[tuple[Heartbeat, dict[str, respx.Route]]]:
    with respx.mock(assert_all_called=False) as router, httpx.Client() as client:
        yield (
            Heartbeat(HC_URL, client),
            {
                "ok": router.post(HC_URL).respond(200),
                "fail": router.post(f"{HC_URL}/fail").respond(200),
            },
        )


def _pinged(route: respx.Route) -> list[str]:
    # `CallList` is an untyped `list`; its elements are `Call`s.
    calls: list[Call] = list(route.calls)
    return [call.request.content.decode() for call in calls]


def test_a_missed_week_pings_success_once(
    engine: Engine, pings: tuple[Heartbeat, dict[str, respx.Route]]
) -> None:
    heartbeat, routes = pings
    clock = FakeClock(et(2026, 11, 17, 13))
    scheduler = _scheduler(clock, engine, heartbeat=heartbeat)

    scheduler.cycle()
    clock.advance(timedelta(hours=1))
    scheduler.cycle()

    assert _status(engine) == "skipped_missed"
    assert _pinged(routes["ok"]) == [f"skipped_missed {RUN_ID} -"]
    assert not routes["fail"].called


def test_a_run_stopped_before_its_row_opened_pings_fail_with_the_reason(
    engine: Engine, pings: tuple[Heartbeat, dict[str, respx.Route]]
) -> None:
    heartbeat, routes = pings
    clock = FakeClock(et(2026, 11, 16, 9))
    fake_run = FakeRun(clock, engine, error=RuntimeError("no transport is configured"))

    _scheduler(clock, engine, run_week=fake_run, heartbeat=heartbeat).cycle()

    assert _status(engine) == "failed"
    assert _pinged(routes["fail"]) == [
        f"failed {RUN_ID} -: RuntimeError: no transport is configured"
    ]
    assert not routes["ok"].called


def test_a_failed_run_that_recorded_itself_is_not_pinged_again(
    engine: Engine, pings: tuple[Heartbeat, dict[str, respx.Route]]
) -> None:
    # `run` pings its own failure once its row is open; the scheduler must not add a second.
    heartbeat, routes = pings
    clock = FakeClock(et(2026, 11, 16, 9))

    class RecordsThenRaises(FakeRun):
        def __call__(self, monday: date) -> None:
            _record(engine, "failed", at=et(2026, 11, 16, 8))
            raise RuntimeError("boom")

    _scheduler(
        clock, engine, run_week=RecordsThenRaises(clock, engine), heartbeat=heartbeat
    ).cycle()

    assert not routes["fail"].called
    assert not routes["ok"].called


def test_a_week_that_ran_is_left_to_run_to_ping(
    engine: Engine, pings: tuple[Heartbeat, dict[str, respx.Route]]
) -> None:
    heartbeat, routes = pings
    clock = FakeClock(et(2026, 11, 16, 9))

    _scheduler(clock, engine, heartbeat=heartbeat).cycle()

    assert _status(engine) == "sent"
    assert not routes["ok"].called
    assert not routes["fail"].called
