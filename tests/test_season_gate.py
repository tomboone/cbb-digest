"""The in-season rule (spec §8.2) and the out-of-season finish."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert, select

from cbb_digest import db
from cbb_digest.runs import start_run
from cbb_digest.schema import games, player_games, players, runs, teams
from cbb_digest.season_gate import finish_if_offseason, is_in_season
from cbb_digest.weeks import EASTERN, windows_for

SEASON = 2027
OPENING_MONDAY = date(2026, 11, 2)
OPENING_TIP = date(2026, 11, 3)
TITLE_MONDAY = date(2027, 4, 5)  # The title game is that night.
AFTER_TITLE = date(2027, 4, 12)
OFFSEASON = date(2027, 4, 19)
NOW = datetime(2027, 4, 19, 11, 0, tzinfo=UTC)

BOTH = {"game_logs_fetched": True, "espn_fetched": True}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            insert(teams),
            [
                {"slug": "kansas", "cbbd_id": 1, "cbbd_name": "Kansas"},
                {"slug": "duke", "cbbd_id": 2, "cbbd_name": "Duke"},
            ],
        )
        connection.execute(
            insert(players).values(
                season=SEASON,
                torvik_pid=1,
                name="Tyran Stokes",
                normalized_name="tyran stokes",
                team_slug="kansas",
                games=1,
                min=30.0,
                pts=10,
                reb=5,
                ast=3,
                fga=8,
                fta=2,
            )
        )
    yield engine
    engine.dispose()


def _game_log(connection: Connection, day: date) -> None:
    connection.execute(
        insert(player_games).values(
            season=SEASON,
            torvik_pid=1,
            game_date=day,
            opponent_slug="duke",
            min=30.0,
            pts=10,
            reb=5,
            ast=3,
            fgm=4,
            fga=8,
            fg3m=1,
            fg3a=3,
            fta=2,
        )
    )


def _espn_game(
    connection: Connection,
    espn_id: str,
    tip: datetime,
    home: str | None = "kansas",
    away: str | None = "duke",
) -> None:
    connection.execute(
        insert(games).values(
            espn_id=espn_id,
            season=SEASON,
            start_utc=tip,
            home_slug=home,
            away_slug=away,
            networks=[],
        )
    )


def _eastern(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), EASTERN)


def _in_season(engine: Engine, monday: date, **fetched: bool) -> bool | None:
    with engine.connect() as connection:
        return is_in_season(connection, monday, **(BOTH | fetched))


@pytest.fixture
def full_season(engine: Engine) -> Engine:
    """Opening night, and the title game; the loader keeps both rows all season."""
    with engine.begin() as connection:
        _espn_game(connection, "opener", _eastern(OPENING_TIP, 19))
        _espn_game(connection, "title", _eastern(TITLE_MONDAY, 21))
        _game_log(connection, OPENING_TIP)
        _game_log(connection, TITLE_MONDAY)
    return engine


def test_the_monday_of_opening_week_is_in_season(full_season: Engine) -> None:
    assert _in_season(full_season, OPENING_MONDAY) is True


def test_the_monday_before_opening_week_is_not(full_season: Engine) -> None:
    assert _in_season(full_season, date(2026, 10, 26)) is False


def test_the_monday_after_the_title_game_is_in_season(full_season: Engine) -> None:
    assert _in_season(full_season, AFTER_TITLE) is True


def test_the_monday_after_that_is_not(full_season: Engine) -> None:
    assert _in_season(full_season, OFFSEASON) is False


def test_a_game_late_on_this_weeks_sunday_counts(engine: Engine) -> None:
    # 23:30 Eastern on Sunday is already Monday in UTC.
    with engine.begin() as connection:
        _espn_game(connection, "late", _eastern(date(2026, 11, 8), 23, 30))
    assert _in_season(engine, OPENING_MONDAY) is True


def test_a_game_at_midnight_after_this_week_does_not(engine: Engine) -> None:
    with engine.begin() as connection:
        _espn_game(connection, "next", _eastern(date(2026, 11, 9), 0))
    assert _in_season(engine, OPENING_MONDAY) is False


def test_a_game_log_dated_this_monday_does_not_count_for_last_week(engine: Engine) -> None:
    with engine.begin() as connection:
        _game_log(connection, OPENING_MONDAY)
    assert _in_season(engine, OPENING_MONDAY) is False


def test_any_scoreboard_game_counts_even_with_both_sides_unmapped(engine: Engine) -> None:
    with engine.begin() as connection:
        _espn_game(connection, "gap", _eastern(OPENING_TIP, 19), home=None, away=None)
    assert _in_season(engine, OPENING_MONDAY) is True


def test_a_source_that_failed_is_ignored_even_with_rows(full_season: Engine) -> None:
    # Each Monday's deciding row belongs to the source whose fetch this run lost, as if an
    # earlier fetch of the week had left it; the other source alone finds nothing.
    assert _in_season(full_season, AFTER_TITLE, game_logs_fetched=False) is False
    assert _in_season(full_season, OPENING_MONDAY, espn_fetched=False) is False


def test_one_fetched_source_finding_a_game_is_enough(full_season: Engine) -> None:
    assert _in_season(full_season, OPENING_MONDAY, game_logs_fetched=False) is True
    assert _in_season(full_season, AFTER_TITLE, espn_fetched=False) is True


def test_one_fetched_source_decides_out_of_season(full_season: Engine) -> None:
    assert _in_season(full_season, OFFSEASON, espn_fetched=False) is False
    assert _in_season(full_season, OFFSEASON, game_logs_fetched=False) is False


def test_with_both_sources_failed_the_gate_is_unknown(full_season: Engine) -> None:
    neither = {"game_logs_fetched": False, "espn_fetched": False}
    assert _in_season(full_season, OPENING_MONDAY, **neither) is None


# --- finish_if_offseason ---


def _start(connection: Connection, monday: date) -> str:
    run_id = f"{SEASON}-W{monday.isocalendar().week:02d}"
    start_run(
        connection,
        run_id=run_id,
        kind="digest",
        season=SEASON,
        windows=windows_for(monday),
        image_sha="abc1234",
        now=NOW,
    )
    return run_id


def test_an_out_of_season_monday_finishes_as_skipped_offseason(full_season: Engine) -> None:
    with full_season.begin() as connection:
        run_id = _start(connection, OFFSEASON)
        skipped = finish_if_offseason(
            connection,
            run_id=run_id,
            monday=OFFSEASON,
            row_counts={"games": 0},
            failed_sources=["cbbd ratings"],
            now=NOW,
            **BOTH,
        )
        row = connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
    assert skipped is True
    assert row["status"] == "skipped_offseason"
    assert row["finished_at"] == NOW
    assert row["row_counts"] == {"games": 0}
    assert row["failed_sources"] == ["cbbd ratings"]
    assert row["sent_at"] is None


@pytest.mark.parametrize(
    ("monday", "fetched"),
    [(AFTER_TITLE, BOTH), (OFFSEASON, {"game_logs_fetched": False, "espn_fetched": False})],
)
def test_an_in_season_or_unknown_monday_is_left_running(
    full_season: Engine, monday: date, fetched: dict[str, bool]
) -> None:
    with full_season.begin() as connection:
        run_id = _start(connection, monday)
        skipped = finish_if_offseason(
            connection,
            run_id=run_id,
            monday=monday,
            row_counts={},
            failed_sources=[],
            now=NOW,
            **fetched,
        )
        status = connection.execute(select(runs.c.status)).scalar_one()
    assert skipped is False
    assert status == "running"
