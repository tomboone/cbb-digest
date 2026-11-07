"""A run's week: its Monday, its two windows and its run id (spec §8.1).

A run belongs to one Monday *M*. **Last week** is *M*-7 00:00 through *M*-1 23:59:59 and **this
week** is *M* 00:00 through *M*+6 23:59:59, both in `America/New_York`. The bounds are built from
local dates and wall times, never by adding hours, so a DST change inside a window leaves both
ends at their Eastern wall time.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
_DAY_END = time(23, 59, 59)


@dataclass(frozen=True, slots=True)
class Window:
    """From `start` through `end`, both inclusive and aware Eastern."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Windows:
    last_week: Window
    this_week: Window


def monday_of(day: date) -> date:
    """The Monday on or before `day`: the week `--week YYYY-MM-DD` names."""
    return day - timedelta(days=day.weekday())


def windows_for(monday: date) -> Windows:
    """Last week's and this week's bounds for a run on `monday`."""
    if monday.weekday() != 0:
        raise ValueError(f"{monday} is not a Monday")
    return Windows(
        last_week=_week_from(monday - timedelta(days=7)),
        this_week=_week_from(monday),
    )


def run_id_for(monday: date, season: int) -> str:
    """`<season>-W<ISO week of monday>`, e.g. `2027-W46`; the week is two digits (`W01`).

    A season runs November to April, so no ISO week number repeats within one.
    """
    return f"{season}-W{monday.isocalendar().week:02d}"


def _week_from(start: date) -> Window:
    return Window(
        start=datetime.combine(start, time(), EASTERN),
        end=datetime.combine(start + timedelta(days=6), _DAY_END, EASTERN),
    )
