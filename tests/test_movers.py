"""Top-25 movers against the previous week's ratings snapshot (spec §8.6)."""

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, delete, insert

from cbb_digest import db
from cbb_digest.compute.models import Mover, MoverKind, MoversSection
from cbb_digest.compute.movers import movers
from cbb_digest.runs import finish_run, start_run
from cbb_digest.schema import team_ratings_snapshot, teams
from cbb_digest.weeks import run_id_for, windows_for

SEASON = 2027
PRESEASON = date(2026, 10, 26)
WEEK_1 = date(2026, 11, 2)
WEEK_2 = date(2026, 11, 9)
WEEK_3 = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)

SLUGS = [f"t{n:02d}" for n in range(1, 41)]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            insert(teams),
            [
                {"slug": slug, "cbbd_id": n, "cbbd_name": f"Team {slug.upper()}"}
                for n, slug in enumerate(SLUGS, start=1)
            ],
        )
    yield engine
    engine.dispose()


def _swapped(order: Sequence[str], *pairs: tuple[int, int]) -> list[str]:
    """`order` with the teams at each pair of ranks swapped."""
    swapped = list(order)
    for a, b in pairs:
        swapped[a - 1], swapped[b - 1] = swapped[b - 1], swapped[a - 1]
    return swapped


def _snapshot(
    connection: Connection, monday: date, order: Sequence[str], *, season: int = SEASON
) -> str:
    """Open the run for `monday` and (re)write its snapshot: rank N is `order[N - 1]`."""
    run_id = run_id_for(monday, season)
    start_run(
        connection,
        run_id=run_id,
        kind="digest",
        season=season,
        windows=windows_for(monday),
        image_sha=None,
        now=NOW,
    )
    connection.execute(
        delete(team_ratings_snapshot).where(team_ratings_snapshot.c.run_id == run_id)
    )
    connection.execute(
        insert(team_ratings_snapshot),
        [
            {
                "run_id": run_id,
                "slug": slug,
                "rank": rank,
                "net": 40.0 - rank,
                "adj_o": 110.0,
                "adj_d": 100.0,
                "fetched_at": NOW,
            }
            for rank, slug in enumerate(order, start=1)
        ],
    )
    return run_id


def _row(slug: str, rank: int | None, prev_rank: int | None, kind: MoverKind) -> Mover:
    change = None if rank is None or prev_rank is None else prev_rank - rank
    return Mover.model_validate(
        {
            "slug": slug,
            "display_name": f"Team {slug.upper()}",
            "rank": rank,
            "prev_rank": prev_rank,
            "change": change,
            "kind": kind,
        }
    )


def test_a_move_of_three_appears_and_a_move_of_two_does_not(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        run_id = _snapshot(connection, WEEK_2, _swapped(SLUGS, (5, 8), (12, 14)))

        section = movers(connection, run_id, SEASON)

    assert section == MoversSection(rows=[_row("t08", 5, 8, "moved"), _row("t05", 8, 5, "moved")])
    assert [row.change for row in section.rows] == [3, -3]


def test_a_team_entering_at_22_and_one_leaving_from_24(engine: Engine) -> None:
    previous = SLUGS[:30]
    # t31 is new at 22, pushing t22 and t23 down one; t24 drops to 31.
    current = [*SLUGS[:21], "t31", "t22", "t23", *SLUGS[24:30], "t24"]
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, previous)
        run_id = _snapshot(connection, WEEK_2, current)

        section = movers(connection, run_id, SEASON)

    assert section.rows == [_row("t31", 22, None, "entered"), _row("t24", 31, 24, "left")]
    assert not section.baseline


def test_moves_come_first_by_size_then_entries_and_exits_by_current_rank(
    engine: Engine,
) -> None:
    current = _swapped(SLUGS, (2, 8), (10, 13))
    current.remove("t25")  # Gone from this week's ratings: everyone below moves up one.
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        run_id = _snapshot(connection, WEEK_2, current)

        section = movers(connection, run_id, SEASON)

    assert section.rows == [
        _row("t08", 2, 8, "moved"),
        _row("t02", 8, 2, "moved"),
        _row("t13", 10, 13, "moved"),
        _row("t10", 13, 10, "moved"),
        _row("t26", 25, 26, "entered"),
        _row("t25", None, 25, "left"),
    ]


def test_a_team_outside_both_top_25s_never_appears(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        run_id = _snapshot(connection, WEEK_2, _swapped(SLUGS, (27, 40)))

        assert movers(connection, run_id, SEASON) == MoversSection()


def test_twelve_qualifying_rows_show_ten_and_count_two_more(engine: Engine) -> None:
    current = _swapped(SLUGS, (1, 4), (2, 5), (3, 6), (7, 10), (8, 11), (9, 12))
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        run_id = _snapshot(connection, WEEK_2, current)

        section = movers(connection, run_id, SEASON)

    assert len(section.rows) == 10
    assert section.more_count == 2
    assert [row.rank for row in section.rows] == list(range(1, 11))


def test_the_first_week_of_a_season_is_the_baseline(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, date(2026, 3, 30), SLUGS, season=2026)  # Last season.
        preseason = _snapshot(connection, PRESEASON, SLUGS)
        finish_run(
            connection,
            run_id=preseason,
            status="skipped_offseason",
            row_counts={},
            failed_sources=[],
            alerts=[],
            transport=None,
            now=NOW,
        )
        run_id = _snapshot(connection, WEEK_1, _swapped(SLUGS, (1, 20)))

        assert movers(connection, run_id, SEASON) == MoversSection(baseline=True)


def test_a_forced_rerun_of_a_week_diffs_against_the_week_before(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        _snapshot(connection, WEEK_2, SLUGS)
        _snapshot(connection, WEEK_3, _swapped(SLUGS, (1, 9)))
        # Week 2 again, after week 3 ran, with fresh ratings.
        run_id = _snapshot(connection, WEEK_2, _swapped(SLUGS, (3, 7)))

        section = movers(connection, run_id, SEASON)

    assert section.rows == [_row("t07", 3, 7, "moved"), _row("t03", 7, 3, "moved")]


def test_a_rerun_of_the_first_week_is_still_the_baseline(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        _snapshot(connection, WEEK_2, SLUGS)
        run_id = _snapshot(connection, WEEK_1, _swapped(SLUGS, (1, 9)))

        assert movers(connection, run_id, SEASON) == MoversSection(baseline=True)


def test_the_previous_week_is_found_across_new_year(engine: Engine) -> None:
    # ISO weeks 52 and 53 of 2026, then week 1 of 2027: run ids no longer sort by date.
    with engine.begin() as connection:
        _snapshot(connection, date(2026, 12, 21), _swapped(SLUGS, (1, 9)))
        _snapshot(connection, date(2026, 12, 28), SLUGS)
        run_id = _snapshot(connection, date(2027, 1, 4), _swapped(SLUGS, (4, 8)))

        section = movers(connection, run_id, SEASON)

    assert run_id == "2027-W01"
    assert section.rows == [_row("t08", 4, 8, "moved"), _row("t04", 8, 4, "moved")]


def test_a_run_without_a_snapshot_is_refused(engine: Engine) -> None:
    with engine.begin() as connection:
        _snapshot(connection, WEEK_1, SLUGS)
        run_id = _snapshot(connection, WEEK_2, SLUGS)
        connection.execute(
            delete(team_ratings_snapshot).where(team_ratings_snapshot.c.run_id == run_id)
        )

        with pytest.raises(LookupError):
            movers(connection, run_id, SEASON)


def test_a_season_that_is_not_the_runs_is_refused(engine: Engine) -> None:
    with engine.begin() as connection:
        run_id = _snapshot(connection, WEEK_1, SLUGS)

        with pytest.raises(ValueError, match="season"):
            movers(connection, run_id, SEASON + 1)
