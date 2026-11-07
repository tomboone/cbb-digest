from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cbb_digest.weeks import monday_of, run_id_for, windows_for

EASTERN = ZoneInfo("America/New_York")


def test_last_week_is_the_seven_days_before_the_monday() -> None:
    windows = windows_for(date(2026, 11, 9))

    assert windows.last_week.start == datetime(2026, 11, 2, tzinfo=EASTERN)
    assert windows.last_week.end == datetime(2026, 11, 8, 23, 59, 59, tzinfo=EASTERN)


def test_this_week_is_the_monday_and_the_six_days_after() -> None:
    windows = windows_for(date(2026, 11, 9))

    assert windows.this_week.start == datetime(2026, 11, 9, tzinfo=EASTERN)
    assert windows.this_week.end == datetime(2026, 11, 15, 23, 59, 59, tzinfo=EASTERN)


def test_a_window_across_the_dst_change_keeps_eastern_wall_times() -> None:
    # DST ended on Sunday 2026-11-01, inside the last week of a run on 2026-11-02.
    last_week = windows_for(date(2026, 11, 2)).last_week

    assert last_week.start.utcoffset() == timedelta(hours=-4)
    assert last_week.end.utcoffset() == timedelta(hours=-5)
    # The fall-back hour makes the window an hour longer in real time.
    elapsed = last_week.end.astimezone(UTC) - last_week.start.astimezone(UTC)
    assert elapsed == timedelta(days=7, hours=1, seconds=-1)


def test_windows_refuse_a_day_that_is_not_a_monday() -> None:
    with pytest.raises(ValueError, match="not a Monday"):
        windows_for(date(2026, 11, 10))


def test_run_id_is_the_season_and_the_iso_week() -> None:
    assert run_id_for(date(2026, 11, 9), 2027) == "2027-W46"


def test_run_id_pads_a_one_digit_week() -> None:
    assert run_id_for(date(2027, 1, 4), 2027) == "2027-W01"


@pytest.mark.parametrize(
    ("day", "monday"),
    [
        (date(2026, 11, 9), date(2026, 11, 9)),
        (date(2026, 11, 11), date(2026, 11, 9)),
        (date(2026, 11, 15), date(2026, 11, 9)),
        (date(2027, 1, 1), date(2026, 12, 28)),
    ],
)
def test_any_date_resolves_to_its_weeks_monday(day: date, monday: date) -> None:
    assert monday_of(day) == monday
