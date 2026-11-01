"""Featured games: this week's games involving the watchlist, in a predictable order (spec §8.3).

A candidate is a game in this run's **this week** where at least one team is the current team of
a `resolved` watchlist player, as `resolve` last stored it. Candidates sort by:

1. the best (lowest) tier among the watchlist players in the game ↑;
2. the number of resolved watchlist players on either team ↓;
3. the worse (numerically higher) of the two teams' ranks ↑: a game is as good as its weaker
   team, and an unranked or unmapped team counts as 999;
4. tip time ↑, then ESPN id, so equal games keep one order from run to run.

The reader picks a game a week from this list, so the order must be one they can predict; a
weighted score, or opponent rank ahead of player count, were both rejected for that reason. Ranks
are this run's ratings snapshot. A player on two watchlist entries counts once, at their best
tier.
"""

from collections import defaultdict
from datetime import datetime

from sqlalchemy import Connection, or_, select

from cbb_digest.compute.models import (
    FeaturedGame,
    FeaturedGamesSection,
    FeaturedPlayer,
    FeaturedTeam,
)
from cbb_digest.resolve import resolved_players
from cbb_digest.schema import games, runs, team_ratings_snapshot, teams
from cbb_digest.watchlist import Watchlist
from cbb_digest.weeks import EASTERN

MAX_ROWS = 5
UNRANKED = 999
NO_BROADCAST = "—"
NETWORK_SEPARATOR = "/"


def featured_games(
    connection: Connection, run_id: str, watchlist: Watchlist, *, ranked: bool = True
) -> FeaturedGamesSection:
    """The featured-games section for `run_id`, from the games and resolutions already stored.

    Pass `ranked=False` when this run's CBBD fetch failed (§9.3): every team then counts as
    unranked, rather than taking a rank from a snapshot an earlier attempt at the run id left.
    The caller omits the section when ESPN failed.
    """
    week_start, week_end = _this_week(connection, run_id, watchlist.season)
    on_team = _players_by_team(connection, watchlist)
    ranks = _ranks(connection, run_id) if ranked else {}
    names = _display_names(connection)

    slugs = list(on_team)
    query = select(games).where(
        games.c.season == watchlist.season,
        games.c.start_utc >= week_start,
        games.c.start_utc <= week_end,
        or_(games.c.home_slug.in_(slugs), games.c.away_slug.in_(slugs)),
    )
    candidates: list[tuple[tuple[int, int, int, datetime, str], FeaturedGame]] = []
    for game in connection.execute(query):
        involved = sorted(
            (
                player
                for slug in (game.home_slug, game.away_slug)
                if slug is not None
                for player in on_team.get(slug, {}).values()
            ),
            key=lambda player: (player.tier, player.name, player.torvik_pid),
        )
        home = _team(game.home_slug, game.home_name, names, ranks)
        away = _team(game.away_slug, game.away_name, names, ranks)
        weaker = max(_rank_or_unranked(home), _rank_or_unranked(away))
        order = (involved[0].tier, -len(involved), weaker, game.start_utc, game.espn_id)
        row = FeaturedGame(
            espn_id=game.espn_id,
            start=game.start_utc.astimezone(EASTERN),
            networks=NETWORK_SEPARATOR.join(game.networks) or NO_BROADCAST,
            venue=game.venue,
            home=home,
            away=away,
            players=involved,
        )
        candidates.append((order, row))

    candidates.sort(key=lambda candidate: candidate[0])
    rows = [row for _, row in candidates]
    return FeaturedGamesSection(rows=rows[:MAX_ROWS], more_count=max(0, len(rows) - MAX_ROWS))


def _this_week(connection: Connection, run_id: str, season: int) -> tuple[datetime, datetime]:
    run = connection.execute(
        select(runs.c.season, runs.c.this_week_start, runs.c.this_week_end).where(
            runs.c.run_id == run_id
        )
    ).one_or_none()
    if run is None or run.this_week_start is None or run.this_week_end is None:
        raise LookupError(f"no digest run {run_id!r} with windows")
    if run.season != season:
        raise ValueError(f"run {run_id!r} is in season {run.season}, not {season}")
    return run.this_week_start, run.this_week_end


def _players_by_team(
    connection: Connection, watchlist: Watchlist
) -> dict[str, dict[int, FeaturedPlayer]]:
    """Team slug → Torvik id → the resolved watchlist player, at their best tier."""
    on_team: dict[str, dict[int, FeaturedPlayer]] = defaultdict(dict)
    for resolved in resolved_players(connection, watchlist):
        team = on_team[resolved.team_slug]
        known = team.get(resolved.torvik_pid)
        if known is None or resolved.player.tier < known.tier:
            team[resolved.torvik_pid] = FeaturedPlayer(
                name=resolved.torvik_name,
                torvik_pid=resolved.torvik_pid,
                tier=resolved.player.tier,
                team_slug=resolved.team_slug,
            )
    return dict(on_team)


def _team(
    slug: str | None, espn_name: str | None, names: dict[str, str], ranks: dict[str, int]
) -> FeaturedTeam:
    """A side under its CBBD name, or under ESPN's when the crosswalk does not know it (§7.5)."""
    if slug is None:
        return FeaturedTeam(slug=None, display_name=espn_name, rank=None)
    return FeaturedTeam(slug=slug, display_name=names[slug], rank=ranks.get(slug))


def _rank_or_unranked(team: FeaturedTeam) -> int:
    return UNRANKED if team.rank is None else team.rank


def _ranks(connection: Connection, run_id: str) -> dict[str, int]:
    query = select(team_ratings_snapshot.c.slug, team_ratings_snapshot.c.rank).where(
        team_ratings_snapshot.c.run_id == run_id
    )
    return {slug: rank for slug, rank in connection.execute(query)}


def _display_names(connection: Connection) -> dict[str, str]:
    return {
        slug: name for slug, name in connection.execute(select(teams.c.slug, teams.c.cbbd_name))
    }
