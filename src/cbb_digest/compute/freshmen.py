"""Freshmen to watch: the freshmen the watchlist does not know about yet (spec §5, §8.5).

The candidates are this season's players in Torvik's season file with class year `Fr`, on a team
ranked 100 or better in this run's ratings snapshot, and not a `resolved` watchlist player. A team
missing from the snapshot has no rank and so does not qualify. They sort by season-to-date total
minutes ↓, then name and Torvik id so ties come out the same way every run, and the top 5 are
shown. There is no `+N more`.
"""

from typing import Any

from sqlalchemy import Connection, Row, select

from cbb_digest.compute.models import FreshmanRow, FreshmenSection
from cbb_digest.compute.watchlist_week import true_shooting
from cbb_digest.resolve import resolved_players
from cbb_digest.schema import players, runs, team_ratings_snapshot, teams
from cbb_digest.watchlist import Watchlist

CLASS_YEAR = "Fr"
MAX_RANK = 100
MAX_ROWS = 5


def freshmen_to_watch(connection: Connection, run_id: str, watchlist: Watchlist) -> FreshmenSection:
    """The freshmen-to-watch section for `run_id`, read after its snapshot is loaded.

    The caller omits the section when this run's Torvik or CBBD fetch failed (§9.3). An empty
    snapshot is CBBD before the season's first games: every team is unranked, so no freshman
    qualifies (§6).
    """
    season = watchlist.season
    _check_run(connection, run_id, season)
    listed = {entry.torvik_pid for entry in resolved_players(connection, watchlist)}

    query = (
        select(
            players.c.torvik_pid,
            players.c.name,
            players.c.team_slug,
            players.c.games,
            players.c.min,
            players.c.pts,
            players.c.reb,
            players.c.ast,
            players.c.fga,
            players.c.fta,
            teams.c.cbbd_name,
            team_ratings_snapshot.c.rank,
        )
        .join(teams, teams.c.slug == players.c.team_slug)
        .join(
            team_ratings_snapshot,
            (team_ratings_snapshot.c.slug == players.c.team_slug)
            & (team_ratings_snapshot.c.run_id == run_id),
        )
        .where(
            players.c.season == season,
            players.c.class_year == CLASS_YEAR,
            players.c.torvik_pid.not_in(listed),
            team_ratings_snapshot.c.rank <= MAX_RANK,
        )
        .order_by(players.c.min.desc(), players.c.name, players.c.torvik_pid)
        .limit(MAX_ROWS)
    )
    return FreshmenSection(rows=[_row(row) for row in connection.execute(query)])


def _row(row: Row[*tuple[Any, ...]]) -> FreshmanRow:
    games = row.games

    def per_game(total: float) -> float:
        return total / games if games else 0.0

    return FreshmanRow(
        name=row.name,
        torvik_pid=row.torvik_pid,
        team_slug=row.team_slug,
        team_name=row.cbbd_name,
        team_rank=row.rank,
        games=games,
        minutes_per_game=per_game(row.min),
        points_per_game=per_game(row.pts),
        rebounds_per_game=per_game(row.reb),
        assists_per_game=per_game(row.ast),
        true_shooting=true_shooting(row.pts, row.fga, row.fta),
    )


def _check_run(connection: Connection, run_id: str, season: int) -> None:
    run_season = connection.execute(
        select(runs.c.season).where(runs.c.run_id == run_id)
    ).scalar_one_or_none()
    if run_season is None:
        raise LookupError(f"no digest run {run_id!r}")
    if run_season != season:
        raise ValueError(f"run {run_id!r} is in season {run_season}, not {season}")
