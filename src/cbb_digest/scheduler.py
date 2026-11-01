"""The weekly loop behind `serve` (spec §9.2), adapted from nba-today's daily one.

For now the loop only keeps the heartbeat fresh and logs the next send time: it dispatches
nothing. Running the pipeline at the send time, and the Tuesday-noon catch-up, come later.
"""

import logging
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from cbb_digest import liveness
from cbb_digest.clock import Clock

log = logging.getLogger(__name__)

SEND_TZ = ZoneInfo("America/New_York")
SEND_WEEKDAY = 0  # Monday
SEND_TIME = time(7)
# The longest single sleep, so the heartbeat never goes more than an hour stale.
MAX_SLEEP = timedelta(hours=1)


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
    """Ticks the heartbeat, then sleeps until the next send time or for `MAX_SLEEP`, if sooner.

    Setting `stop` ends the loop at the next sleep.
    """

    def __init__(
        self, clock: Clock, engine: Engine, *, stop: threading.Event | None = None
    ) -> None:
        self._clock = clock
        self._engine = engine
        self._stop = stop or threading.Event()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            self.cycle()

    def cycle(self) -> None:
        now = self._clock.now()
        liveness.record_tick(self._engine, now)
        send_at = next_send_time(now)
        log.info("next send", extra={"send_at": send_at.isoformat()})
        self._clock.sleep_until(min(send_at, now + MAX_SLEEP))
