"""Top-25 movers: this run's ratings snapshot against the previous week's (spec §8.6).

A team qualifies if it is in either top 25 and its rank changed by 3 or more places, or it
entered or left the top 25. A team in neither snapshot's top 25 never qualifies, however far it
moved. Moves come first, by the size of the change, then entries and exits by current rank; a
team that left and is missing from this week's snapshot goes last.

The previous snapshot is the latest digest run in the same season whose week starts before this
run's, so a forced re-run of a week compares with the week before, never with itself, and a
missed week compares with the last one that ran. Runs skipped as out of season do not count:
preseason Mondays still load ratings, and the opening-week digest shows the baseline line
(§8.2). Ordering is by window, not run id, because ISO week numbers restart in January.
"""

from sqlalchemy import Connection, exists, select

from cbb_digest.compute.models import Mover, MoversSection
from cbb_digest.schema import runs, team_ratings_snapshot, teams

TOP = 25
MIN_CHANGE = 3
MAX_ROWS = 10


def movers(connection: Connection, run_id: str, season: int) -> MoversSection:
    """The movers section for `run_id`, whose snapshot must already be loaded.

    The caller omits the section when this run's CBBD fetch failed (§9.3): a snapshot left by an
    earlier attempt at the same run id would otherwise stand in for this week's ratings.
    """
    current = _snapshot(connection, run_id)
    if not current:
        raise LookupError(f"no ratings snapshot for run {run_id!r}")
    previous_run_id = _previous_run_id(connection, run_id, season)
    if previous_run_id is None:
        return MoversSection(baseline=True)
    previous = _snapshot(connection, previous_run_id)
    names = _display_names(connection)

    rows: list[Mover] = []
    for slug in sorted(_top(current) | _top(previous)):
        rank, prev_rank = current.get(slug), previous.get(slug)
        change = None if rank is None or prev_rank is None else prev_rank - rank
        if not _in_top(prev_rank):
            kind = "entered"
        elif not _in_top(rank):
            kind = "left"
        elif change is not None and abs(change) >= MIN_CHANGE:
            kind = "moved"
        else:
            continue
        rows.append(
            Mover(
                slug=slug,
                display_name=names[slug],
                rank=rank,
                prev_rank=prev_rank,
                change=change,
                kind=kind,
            )
        )

    rows.sort(key=_order)
    return MoversSection(rows=rows[:MAX_ROWS], more_count=max(0, len(rows) - MAX_ROWS))


def _order(row: Mover) -> tuple[int, int, int, int]:
    if row.kind == "moved":
        assert row.change is not None and row.rank is not None
        return (0, -abs(row.change), row.rank, 0)
    if row.rank is None:
        assert row.prev_rank is not None
        return (2, 0, 0, row.prev_rank)
    return (1, 0, row.rank, 0)


def _in_top(rank: int | None) -> bool:
    return rank is not None and rank <= TOP


def _top(snapshot: dict[str, int]) -> set[str]:
    return {slug for slug, rank in snapshot.items() if rank <= TOP}


def _snapshot(connection: Connection, run_id: str) -> dict[str, int]:
    query = select(team_ratings_snapshot.c.slug, team_ratings_snapshot.c.rank).where(
        team_ratings_snapshot.c.run_id == run_id
    )
    return {slug: rank for slug, rank in connection.execute(query)}


def _previous_run_id(connection: Connection, run_id: str, season: int) -> str | None:
    run = connection.execute(
        select(runs.c.season, runs.c.this_week_start).where(runs.c.run_id == run_id)
    ).one_or_none()
    if run is None or run.this_week_start is None:
        raise LookupError(f"no digest run {run_id!r} with windows")
    if run.season != season:
        raise ValueError(f"run {run_id!r} is in season {run.season}, not {season}")
    this_week_start = run.this_week_start
    has_snapshot = exists().where(team_ratings_snapshot.c.run_id == runs.c.run_id)
    query = (
        select(runs.c.run_id)
        .where(
            runs.c.season == season,
            runs.c.kind == "digest",
            runs.c.status != "skipped_offseason",
            runs.c.this_week_start < this_week_start,
            has_snapshot,
        )
        .order_by(runs.c.this_week_start.desc())
        .limit(1)
    )
    return connection.execute(query).scalar_one_or_none()


def _display_names(connection: Connection) -> dict[str, str]:
    return {
        slug: name for slug, name in connection.execute(select(teams.c.slug, teams.c.cbbd_name))
    }
