"""The weekly loop behind `serve` (spec §9.2), adapted from nba-today's daily one.

Each cycle ticks the heartbeat, settles the current week, then sleeps until the next send time or
for `MAX_SLEEP`, if sooner. Settling reads the week's `runs` row, so a restart never sends twice
or skips a week (ADR-0001):
- before Monday 07:00 Eastern the week is not yet due;
- from Monday 07:00 until Tuesday 12:00 a week not yet settled is run. A missed Monday catches up;
- from Tuesday 12:00 on, a week with no outcome is recorded `skipped_missed`. Healthchecks has
  already alarmed by then. A `running` row started after the cutoff is a run someone started by
  hand, and is left to finish.

A week is settled once it has gone out by email or was `skipped_offseason`. A dry run's outbox
send emailed nobody, so it settles the week only for a dry-run `serve`: the real one still sends
it (spec §9.2).

A run that fails, by any exception, is logged and recorded `failed`, and the loop goes on. A
failed week is not settled, so each later cycle runs it again until Tuesday noon: at most once an
hour, since every cycle sleeps, and never a second email, since a week that went out is settled.
"""

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select

from cbb_digest import liveness
from cbb_digest.clock import Clock
from cbb_digest.runs import (
    SENT_STATUSES,
    FinalStatus,
    already_sent,
    finish_run,
    run_status,
    start_run,
)
from cbb_digest.schema import runs
from cbb_digest.weeks import monday_of, run_id_for, windows_for

log = logging.getLogger(__name__)

SEND_TZ = ZoneInfo("America/New_York")
SEND_WEEKDAY = 0  # Monday
SEND_TIME = time(7)
# A week not sent by Tuesday noon is skipped rather than caught up.
CATCH_UP_DAYS = 1
CATCH_UP_UNTIL = time(12)
# The longest single sleep, so the heartbeat never goes more than an hour stale.
MAX_SLEEP = timedelta(hours=1)

# Runs one week end to end, given its Monday: `cbb-digest run --week <monday>`.
type RunWeek = Callable[[date], object]


def next_send_time(now: datetime) -> datetime:
    """The first Monday 07:00 `America/New_York` strictly after `now`, in that zone.

    Built from a local date and wall time rather than by adding hours, so a DST change in
    between leaves it at 07:00 local.
    """
    if now.tzinfo is None:
        raise ValueError(f"naive datetime {now!r}: pass an aware datetime")
    local = now.astimezone(SEND_TZ)
    days_ahead = (SEND_WEEKDAY - local.weekday()) % 7
    candidate = datetime.combine(local.date() + timedelta(days=days_ahead), SEND_TIME, SEND_TZ)
    if candidate <= local:
        candidate = datetime.combine(candidate.date() + timedelta(days=7), SEND_TIME, SEND_TZ)
    return candidate


class Scheduler:
    """Ticks the heartbeat, settles the current week, then sleeps until the next send time or
    for `MAX_SLEEP`, if sooner.

    `run_week` runs a week given its Monday; `season` names its run id. `dry_run` says whether
    `run_week` sends to the outbox. Setting `stop` ends the loop at the next sleep.
    """

    def __init__(
        self,
        clock: Clock,
        engine: Engine,
        *,
        season: int,
        run_week: RunWeek,
        dry_run: bool = False,
        image_sha: str | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._clock = clock
        self._engine = engine
        self._season = season
        self._run_week = run_week
        self._dry_run = dry_run
        self._image_sha = image_sha
        self._stop = stop or threading.Event()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.cycle()

    def cycle(self) -> None:
        now = self._clock.now()
        liveness.record_tick(self._engine, now)
        self._settle_week(now)
        # A run takes time; sleep from when it finished.
        now = self._clock.now()
        send_at = next_send_time(now)
        log.info("next send", extra={"send_at": send_at.isoformat()})
        self._clock.sleep_until(min(send_at, now + MAX_SLEEP))

    def _settle_week(self, now: datetime) -> None:
        monday = monday_of(now.astimezone(SEND_TZ).date())
        run_id = run_id_for(monday, self._season)
        due = datetime.combine(monday, SEND_TIME, SEND_TZ)
        cutoff = datetime.combine(monday + timedelta(days=CATCH_UP_DAYS), CATCH_UP_UNTIL, SEND_TZ)
        if now < due:
            return
        if now >= cutoff:
            self._record_missed(monday, run_id, cutoff, now)
        elif not self._settled(run_id):
            self._dispatch(monday, run_id)

    def _settled(self, run_id: str) -> bool:
        with self._engine.connect() as connection:
            if already_sent(connection, run_id):
                return True
            status = run_status(connection, run_id)
        return status == "skipped_offseason" or (self._dry_run and status in SENT_STATUSES)

    def _dispatch(self, monday: date, run_id: str) -> None:
        log.info("running the week", extra={"run_id": run_id})
        try:
            self._run_week(monday)
        except Exception:
            # `run` records its own failures once its row is open; this covers what stops it
            # before then (a bad watchlist or transport config), so the week still shows failed.
            log.exception("scheduled run failed", extra={"run_id": run_id})
            self._close_open(monday, run_id, "failed", self._clock.now())

    def _record_missed(self, monday: date, run_id: str, cutoff: datetime, now: datetime) -> None:
        with self._engine.connect() as connection:
            if already_sent(connection, run_id):
                # Emailed, then died before finishing: not missed.
                return
            started_at = connection.execute(
                select(runs.c.started_at).where(runs.c.run_id == run_id)
            ).scalar_one_or_none()
        if started_at is not None and started_at >= cutoff:
            # A run started by hand after the cutoff; `running` until it finishes. Any outcome
            # it reaches is left alone too, by `_close_open`.
            return
        if self._close_open(monday, run_id, "skipped_missed", now):
            log.warning("week missed: not sent by Tuesday noon", extra={"run_id": run_id})

    def _close_open(self, monday: date, run_id: str, status: FinalStatus, now: datetime) -> bool:
        """Record `run_id` as `status` if it has no outcome yet (no row, or still `running`);
        whether it did."""
        with self._engine.begin() as connection:
            if run_status(connection, run_id) not in (None, "running"):
                return False
            start_run(
                connection,
                run_id=run_id,
                kind="digest",
                season=self._season,
                windows=windows_for(monday),
                image_sha=self._image_sha,
                now=now,
            )
            finish_run(
                connection,
                run_id=run_id,
                status=status,
                row_counts={},
                failed_sources=[],
                alerts=[],
                transport=None,
                now=now,
            )
        return True
