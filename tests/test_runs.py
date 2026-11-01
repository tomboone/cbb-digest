"""The `runs` row's lifecycle (spec §9.4)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from cbb_digest import db
from cbb_digest.runs import ensure_run, finish_run, start_run
from cbb_digest.schema import runs
from cbb_digest.weeks import windows_for

RUN = "2027-W46"
SEASON = 2027
MONDAY = date(2026, 11, 9)
WINDOWS = windows_for(MONDAY)
STARTED = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
FINISHED = STARTED + timedelta(minutes=2, seconds=5)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    yield engine
    engine.dispose()


def _start(engine: Engine, *, now: datetime = STARTED, image_sha: str | None = "abc1234") -> None:
    with engine.begin() as connection:
        start_run(
            connection,
            run_id=RUN,
            kind="digest",
            season=SEASON,
            windows=WINDOWS,
            image_sha=image_sha,
            now=now,
        )


def _finish(engine: Engine, *, now: datetime = FINISHED, **overrides: Any) -> None:
    arguments: dict[str, Any] = {
        "status": "sent_degraded",
        "row_counts": {"players": 412, "games": 63},
        "failed_sources": ["cbbd ratings"],
        "alerts": ["inactive: Tyran Stokes (DNP — 2nd straight week)"],
        "transport": "resend",
    }
    with engine.begin() as connection:
        finish_run(connection, run_id=RUN, now=now, **(arguments | overrides))


def _rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(runs)).mappings()]


def test_start_run_opens_a_running_row(engine: Engine) -> None:
    _start(engine)

    assert _rows(engine) == [
        {
            "run_id": RUN,
            "kind": "digest",
            "season": SEASON,
            "last_week_start": WINDOWS.last_week.start,
            "last_week_end": WINDOWS.last_week.end,
            "this_week_start": WINDOWS.this_week.start,
            "this_week_end": WINDOWS.this_week.end,
            "status": "running",
            "started_at": STARTED,
            "finished_at": None,
            "duration_s": None,
            "row_counts": None,
            "failed_sources": None,
            "alerts": None,
            "image_sha": "abc1234",
            "sent_at": None,
            "transport": None,
        }
    ]


def test_the_row_round_trips_every_field(engine: Engine) -> None:
    _start(engine)
    _finish(engine)

    assert _rows(engine) == [
        {
            "run_id": RUN,
            "kind": "digest",
            "season": SEASON,
            "last_week_start": WINDOWS.last_week.start,
            "last_week_end": WINDOWS.last_week.end,
            "this_week_start": WINDOWS.this_week.start,
            "this_week_end": WINDOWS.this_week.end,
            "status": "sent_degraded",
            "started_at": STARTED,
            "finished_at": FINISHED,
            "duration_s": 125.0,
            "row_counts": {"players": 412, "games": 63},
            "failed_sources": ["cbbd ratings"],
            "alerts": ["inactive: Tyran Stokes (DNP — 2nd straight week)"],
            "image_sha": "abc1234",
            "sent_at": FINISHED,
            "transport": "resend",
        }
    ]


def test_restarting_a_run_id_resets_it_to_running_without_a_second_row(engine: Engine) -> None:
    _start(engine)
    _finish(engine, status="failed", transport=None)
    restarted = FINISHED + timedelta(hours=1)

    _start(engine, now=restarted, image_sha="def5678")

    (row,) = _rows(engine)
    assert row["status"] == "running"
    assert row["started_at"] == restarted
    assert row["image_sha"] == "def5678"
    for column in ("finished_at", "duration_s", "row_counts", "failed_sources", "alerts"):
        assert row[column] is None, column


def test_a_restart_keeps_the_earlier_send(engine: Engine) -> None:
    # Restarting a week that went out must not make it look unsent (spec §9.2).
    _start(engine)
    _finish(engine, status="sent")
    _start(engine, now=FINISHED + timedelta(hours=1))
    _finish(engine, now=FINISHED + timedelta(hours=2), status="failed", transport=None)

    (row,) = _rows(engine)
    assert row["status"] == "failed"
    assert row["sent_at"] == FINISHED
    assert row["transport"] == "resend"


def test_a_forced_resend_moves_sent_at(engine: Engine) -> None:
    _start(engine)
    _finish(engine, status="sent")
    resent = FINISHED + timedelta(hours=2)
    _start(engine, now=resent - timedelta(minutes=1))
    _finish(engine, now=resent, status="sent", transport="smtp")

    (row,) = _rows(engine)
    assert row["sent_at"] == resent
    assert row["transport"] == "smtp"


def test_a_reset_run_has_no_windows(engine: Engine) -> None:
    with engine.begin() as connection:
        start_run(
            connection,
            run_id="reset-2028-2027-04-28",
            kind="reset",
            season=2028,
            windows=None,
            image_sha=None,
            now=STARTED,
        )

    (row,) = _rows(engine)
    assert row["kind"] == "reset"
    assert [row[c] for c in ("last_week_start", "this_week_end", "image_sha")] == [None] * 3


@pytest.mark.parametrize(
    ("status", "transport"),
    [("sent", None), ("sent_degraded", None), ("failed", "resend"), ("skipped_offseason", "smtp")],
)
def test_a_transport_goes_with_a_sent_status_and_only_with_one(
    engine: Engine, status: str, transport: str | None
) -> None:
    _start(engine)
    with pytest.raises(ValueError, match="transport"):
        _finish(engine, status=status, transport=transport)
    assert _rows(engine)[0]["status"] == "running"


def test_finishing_an_unknown_run_raises(engine: Engine) -> None:
    with pytest.raises(LookupError, match=RUN):
        _finish(engine)


def test_ensure_run_leaves_a_started_run_alone(engine: Engine) -> None:
    _start(engine)
    with engine.begin() as connection:
        ensure_run(connection, run_id=RUN, season=SEASON, windows=WINDOWS, now=FINISHED)
        count = connection.execute(select(func.count()).select_from(runs)).scalar_one()

    assert count == 1
    assert _rows(engine)[0]["image_sha"] == "abc1234"
