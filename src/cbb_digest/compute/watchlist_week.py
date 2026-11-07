"""Watchlist last week: each resolved watchlist player's line, and the one flag worth reading
(spec §5, §8.4).

A row goes to every `resolved` watchlist player whose team played during the run's **last week**,
including a player who did not get on the floor: that is the `DNP` row. A player whose team did
not play gets no row. A team played on a date when any player on it, or any opponent of it, has a
game-log row; Torvik writes no row for a player who did not play (§6), so a missing row on such a
date is a DNP, and so is a row with 0 minutes.

The **notable** is the first of these that matches:

1. `DNP`, or `DNP — 2nd straight week` (`3rd`, …) when the player also sat through every week
   before it in which the team played, counting back until a week the team did not play or the
   player did;
2. `Season high: N pts`, when a game last week beat every earlier game of the season in points,
   with at least 3 of them before it;
3. `0-for-N FG` or `0-for-N 3P`, for a game with no makes on 5 or more attempts, showing the
   larger N (FG when they tie).

Rows sort by tier ↑, then minutes per game ↓, then name, and are capped at 40. A player on two
watchlist entries gets one row, at their best tier.

**Inactive** (§7.4): from the second straight DNP week the player is inactive, and the alert
follows the resolution alerts' on-change rule. It is raised on the run that first finds them
inactive, which `watchlist_resolution.inactive_run_id` records, and again on a re-run of that
week, but not while they stay inactive. Only a week in which they play clears it: a week the team
does not play says nothing about the player, and a single DNP week after a break (exams, a bye)
continues the same spell, so neither raises the alert a second time.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Connection, select, update

from cbb_digest.compute.models import WatchlistWeekRow, WatchlistWeekSection
from cbb_digest.resolve import Alert, ResolvedPlayer, resolved_players
from cbb_digest.schema import player_games, players, runs, teams, watchlist_resolution
from cbb_digest.watchlist import Watchlist
from cbb_digest.weeks import EASTERN

MAX_ROWS = 40
SEASON_HIGH_PRIOR_GAMES = 3
ZERO_FOR_ATTEMPTS = 5
INACTIVE_WEEKS = 2
INACTIVE_DETAIL = "no minutes in two straight weeks while the team played"

_WEEK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class _Game:
    """One game a player played, from `player_games`."""

    game_date: date
    minutes: float
    pts: int
    reb: int
    ast: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    fta: int


def true_shooting(pts: int, fga: int, fta: int) -> float | None:
    """TS% as a fraction: PTS / (2 * (FGA + 0.44 * FTA)), or `None` with no attempts (§5)."""
    if fga + fta == 0:
        return None
    return pts / (2 * (fga + 0.44 * fta))


def watchlist_last_week(
    connection: Connection, run_id: str, watchlist: Watchlist
) -> WatchlistWeekSection:
    """The watchlist-last-week section for `run_id`, from the game logs and resolutions stored.

    Stores each listed player's inactive state as it goes, so run it inside the run's
    transaction. The caller omits the section when this run's Torvik fetch failed (§9.3).
    """
    season = watchlist.season
    first_day, last_day = _last_week(connection, run_id, season)
    entries = _best_entries(resolved_players(connection, watchlist))
    team_dates = _team_dates(connection, season, {p.team_slug for p in entries.values()}, last_day)
    played = _games(connection, season, entries, last_day)
    names = _display_names(connection)

    rows: list[WatchlistWeekRow] = []
    alerts: list[Alert] = []
    for pid, entry in entries.items():
        dates = team_dates.get(entry.team_slug, set())
        if not _in_week(dates, first_day):
            continue
        season_games = played.get(pid, [])
        week = [game for game in season_games if game.game_date >= first_day]
        streak = _dnp_streak(dates, {game.game_date for game in season_games}, first_day)
        if _record_inactive(connection, season, pid, streak, run_id):
            alerts.append(Alert("inactive", entry.player.name, INACTIVE_DETAIL))
        rows.append(_row(entry, names[entry.team_slug], week, season_games, streak))

    rows.sort(key=lambda row: (row.tier, -row.minutes_per_game, row.name, row.torvik_pid))
    return WatchlistWeekSection(
        rows=rows[:MAX_ROWS], more_count=max(0, len(rows) - MAX_ROWS), alerts=alerts
    )


def _row(
    entry: ResolvedPlayer,
    team_name: str,
    week: Sequence[_Game],
    season_games: Sequence[_Game],
    streak: int,
) -> WatchlistWeekRow:
    count = len(week)

    def per_game(values: Iterable[float]) -> float:
        return sum(values) / count if count else 0.0

    return WatchlistWeekRow(
        name=entry.torvik_name,
        torvik_pid=entry.torvik_pid,
        tier=entry.player.tier,
        team_slug=entry.team_slug,
        team_name=team_name,
        games=count,
        minutes_per_game=per_game(game.minutes for game in week),
        points_per_game=per_game(game.pts for game in week),
        rebounds_per_game=per_game(game.reb for game in week),
        assists_per_game=per_game(game.ast for game in week),
        true_shooting=true_shooting(
            sum(game.pts for game in week),
            sum(game.fga for game in week),
            sum(game.fta for game in week),
        ),
        threes_attempted_per_game=per_game(game.fg3a for game in week),
        notable=_notable(week, season_games, streak),
    )


def _notable(week: Sequence[_Game], season_games: Sequence[_Game], streak: int) -> str | None:
    if streak == 1:
        return "DNP"
    if streak > 1:
        return f"DNP — {_ordinal(streak)} straight week"
    high = _season_high(week, season_games)
    if high is not None:
        return f"Season high: {high} pts"
    return _zero_for(week)


def _season_high(week: Sequence[_Game], season_games: Sequence[_Game]) -> int | None:
    """Last week's best points, if that game beat every earlier one with enough before it.

    `max` keeps the first of equal games, so a later game that only ties the high sets nothing.
    """
    if not week:
        return None
    best = max(week, key=lambda game: game.pts)
    before = [game.pts for game in season_games if game.game_date < best.game_date]
    if len(before) >= SEASON_HIGH_PRIOR_GAMES and best.pts > max(before):
        return best.pts
    return None


def _zero_for(week: Sequence[_Game]) -> str | None:
    found: list[tuple[int, str]] = []
    for game in week:
        if game.fgm == 0 and game.fga >= ZERO_FOR_ATTEMPTS:
            found.append((game.fga, "FG"))
        if game.fg3m == 0 and game.fg3a >= ZERO_FOR_ATTEMPTS:
            found.append((game.fg3a, "3P"))
    if not found:
        return None
    attempts, label = min(found, key=lambda item: (-item[0], item[1] != "FG"))
    return f"0-for-{attempts} {label}"


def _dnp_streak(team_dates: set[date], played_dates: set[date], first_day: date) -> int:
    """How many weeks in a row, back from last week, the team played and the player did not."""
    streak = 0
    start = first_day
    while _in_week(team_dates, start) and not _in_week(played_dates, start):
        streak += 1
        start -= _WEEK
    return streak


def _in_week(dates: Iterable[date], start: date) -> bool:
    end = start + _WEEK
    return any(start <= day < end for day in dates)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _record_inactive(
    connection: Connection, season: int, pid: int, streak: int, run_id: str
) -> bool:
    """Update the player's stored inactive state from this week's DNP `streak`; whether this run
    raises the `inactive` alert.

    Every resolved entry for the player shares one state. An entry added while the player is
    already inactive takes the run that raised it, so a re-run of that week still raises it.
    """
    where = (
        (watchlist_resolution.c.season == season)
        & (watchlist_resolution.c.torvik_pid == pid)
        & (watchlist_resolution.c.outcome == "resolved")
    )
    column = watchlist_resolution.c.inactive_run_id
    if streak == 0:
        connection.execute(
            update(watchlist_resolution).where(where, column.is_not(None)).values({column: None})
        )
        return False
    if streak < INACTIVE_WEEKS:
        return False
    raised_by = set(connection.execute(select(column).where(where)).scalars()) - {None}
    connection.execute(
        update(watchlist_resolution)
        .where(where, column.is_(None))
        .values({column: min(raised_by, default=run_id)})
    )
    # Raised by this run, unless an earlier run raised it and the player has not played since.
    return raised_by <= {run_id}


def _best_entries(resolved: Iterable[ResolvedPlayer]) -> dict[int, ResolvedPlayer]:
    """Torvik id → the player's watchlist entry with the best tier, in watchlist order."""
    best: dict[int, ResolvedPlayer] = {}
    for entry in resolved:
        known = best.get(entry.torvik_pid)
        if known is None or entry.player.tier < known.player.tier:
            best[entry.torvik_pid] = entry
    return best


def _last_week(connection: Connection, run_id: str, season: int) -> tuple[date, date]:
    """Last week's first and last Eastern dates, which `player_games.game_date` is kept in."""
    run = connection.execute(
        select(runs.c.season, runs.c.last_week_start, runs.c.last_week_end).where(
            runs.c.run_id == run_id
        )
    ).one_or_none()
    if run is None or run.last_week_start is None or run.last_week_end is None:
        raise LookupError(f"no digest run {run_id!r} with windows")
    if run.season != season:
        raise ValueError(f"run {run_id!r} is in season {run.season}, not {season}")
    return (
        run.last_week_start.astimezone(EASTERN).date(),
        run.last_week_end.astimezone(EASTERN).date(),
    )


def _team_dates(
    connection: Connection, season: int, slugs: set[str], last_day: date
) -> dict[str, set[date]]:
    """Team slug → the dates up to `last_day` on which it played, from either side's rows."""
    own = (
        select(players.c.team_slug, player_games.c.game_date)
        .join(
            players,
            (players.c.season == player_games.c.season)
            & (players.c.torvik_pid == player_games.c.torvik_pid),
        )
        .where(
            player_games.c.season == season,
            players.c.team_slug.in_(slugs),
            player_games.c.game_date <= last_day,
        )
    )
    opponents = select(player_games.c.opponent_slug, player_games.c.game_date).where(
        player_games.c.season == season,
        player_games.c.opponent_slug.in_(slugs),
        player_games.c.game_date <= last_day,
    )
    dates: dict[str, set[date]] = defaultdict(set)
    for query in (own, opponents):
        for slug, game_date in connection.execute(query.distinct()):
            dates[slug].add(game_date)
    return dict(dates)


def _games(
    connection: Connection, season: int, entries: dict[int, ResolvedPlayer], last_day: date
) -> dict[int, list[_Game]]:
    """Torvik id → the games up to `last_day` the player played (minutes > 0), by date."""
    query = (
        select(player_games)
        .where(
            player_games.c.season == season,
            player_games.c.torvik_pid.in_(list(entries)),
            player_games.c.game_date <= last_day,
            player_games.c.min > 0,
        )
        .order_by(player_games.c.game_date, player_games.c.opponent_slug)
    )
    found: dict[int, list[_Game]] = defaultdict(list)
    for row in connection.execute(query):
        found[row.torvik_pid].append(
            _Game(
                game_date=row.game_date,
                minutes=row.min,
                pts=row.pts,
                reb=row.reb,
                ast=row.ast,
                fgm=row.fgm,
                fga=row.fga,
                fg3m=row.fg3m,
                fg3a=row.fg3a,
                fta=row.fta,
            )
        )
    return dict(found)


def _display_names(connection: Connection) -> dict[str, str]:
    return {
        slug: name for slug, name in connection.execute(select(teams.c.slug, teams.c.cbbd_name))
    }
