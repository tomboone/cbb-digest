"""The `serve` loop's heartbeat row in SQLite, and the `health` verdict on it (spec §9.1, §9.4).

Not `heartbeat.py`: that name is kept for the healthchecks.io client ported from nba-today
(spec §9.1), which is a different heartbeat — an outbound ping, not a local liveness row.
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from cbb_digest.schema import heartbeat

# `serve` ticks at least hourly; a week plus a day of slack covers a long send on Monday.
MAX_TICK_AGE = timedelta(days=8)

_SINGLETON_ID = 1


def record_tick(engine: Engine, at: datetime) -> None:
    """Store `at` as the latest loop tick, creating the singleton row on the first call."""
    statement = insert(heartbeat).values(id=_SINGLETON_ID, ticked_at=at)
    statement = statement.on_conflict_do_update(
        index_elements=[heartbeat.c.id], set_={"ticked_at": statement.excluded.ticked_at}
    )
    with engine.begin() as connection:
        connection.execute(statement)


def last_tick(engine: Engine) -> datetime | None:
    """The latest loop tick, as aware UTC; `None` if `serve` has never ticked."""
    with engine.connect() as connection:
        return connection.execute(select(heartbeat.c.ticked_at)).scalar_one_or_none()


def is_healthy(ticked_at: datetime, now: datetime) -> bool:
    return now - ticked_at < MAX_TICK_AGE
