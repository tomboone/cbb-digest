"""`cbb-digest load`: the cached sources, upserted into SQLite by natural key (spec §9.1, §9.4).

Re-running a week is harmless: every table is written by `INSERT … ON CONFLICT DO UPDATE` on its
§9.4 key, updating a row only where a column differs, so a second `load` of the same cache
changes zero rows. The ratings snapshot is the one table replaced rather than merged: a run id's
rows are exactly the ratings it last loaded (spec §9.2).

Team names map through the crosswalk in `teams` (§7.5). A source row whose team the crosswalk
does not know is dropped and counted, never guessed. The one exception is `games`: a game keeps
its row with NULL for that side, since a non-D-I opponent is never in the crosswalk (§8.3).

**Renames.** CBBD's id is a team's identity, not its slug. When CBBD renames a school (IUPUI →
IU Indianapolis), the row keeps the slug it was first stored under, because `players`, `games`
and the snapshots reference that slug; its CBBD name, Torvik name and ESPN id are updated. A
Torvik name or ESPN id that moves from one team to another (a swap in `teams_crosswalk.yaml`) is
first cleared from the team that loses it, so the `UNIQUE` columns never hold it twice.

**Crosswalk edits.** `teams_crosswalk.yaml` is re-applied on every load, not only when CBBD's
teams are fetched: without a fresh teams response, the canonical teams are rebuilt from the
stored rows (`stored_cbbd_teams`), so a merged override fixes an `unmapped_team` on the next run.

`espn_name` is ESPN's `location` for the team, filled from the week's games: ESPN is the only
source that carries it, and the crosswalk never sets it. A team that has not played on ESPN's
scoreboard yet keeps NULL.

The `runs` row is not this module's: it inserts a placeholder (`status=running`) only so the
snapshot's foreign key holds, and never changes an existing row.
"""

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Table, delete, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cbb_digest.schema import games, player_games, players, runs, team_ratings_snapshot, teams
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.sources.models.cbbd_rating import CbbdRating, rank_ratings
from cbb_digest.sources.models.cbbd_team import CbbdTeam
from cbb_digest.sources.models.espn_scoreboard import EspnGame, EspnTeam
from cbb_digest.sources.models.torvik_player_game import TorvikPlayerGame
from cbb_digest.sources.models.torvik_player_season import TorvikPlayerSeason
from cbb_digest.teams import CanonicalTeam, Crosswalk
from cbb_digest.watchlist import normalise_name
from cbb_digest.weeks import Windows

TABLES = ("teams", "players", "player_games", "games", "team_ratings_snapshot")

UNMAPPED_TEAM = "unmapped team"
UNMAPPED_OPPONENT = "unmapped opponent"
OTHER_SEASON = "other season"
NO_PLAYER = "no player row"
DUPLICATE = "duplicate"
UNMAPPED_SIDE = "unmapped side stored as NULL"

log = logging.getLogger(__name__)


class LoadError(RuntimeError):
    """The load cannot start, e.g. no canonical teams to map anything with."""


@dataclass(slots=True)
class TableReport:
    changed: int = 0
    # Rows left out, or stored differently, by reason.
    notes: Counter[str] = field(default_factory=Counter[str])


@dataclass(slots=True)
class LoadReport:
    # The tables loaded; one whose source failed is absent, and left as it was.
    tables: dict[str, TableReport] = field(default_factory=dict[str, TableReport])


@dataclass(frozen=True, slots=True)
class LoadInput:
    season: int
    run_id: str
    windows: Windows
    # `None` when CBBD's teams were not fetched: `teams` is used as it stands.
    canonical_teams: Sequence[CanonicalTeam] | None
    player_season: Fetched[list[TorvikPlayerSeason]]
    player_games: Fetched[list[TorvikPlayerGame]]
    ratings: Fetched[list[CbbdRating]]
    # When the ratings response was fetched; required when `ratings` is not a failure.
    ratings_fetched_at: datetime | None
    espn_week: Fetched[list[EspnGame]]


def load(connection: Connection, data: LoadInput, *, now: datetime) -> LoadReport:
    """Upsert every source in `data` that is not a failure. Run it inside one transaction."""
    report = LoadReport()
    espn_week = None if isinstance(data.espn_week, SourceFailure) else data.espn_week
    report.tables["teams"] = _load_teams(connection, data.canonical_teams, espn_week)
    crosswalk = _crosswalk(connection)

    if not isinstance(data.player_season, SourceFailure):
        report.tables["players"] = _load_players(
            connection, crosswalk, data.season, data.player_season
        )
    if not isinstance(data.player_games, SourceFailure):
        report.tables["player_games"] = _load_player_games(
            connection, crosswalk, data.season, data.player_games
        )
    if espn_week is not None:
        report.tables["games"] = _load_games(connection, crosswalk, data.season, espn_week)
    if not isinstance(data.ratings, SourceFailure):
        if data.ratings_fetched_at is None:
            raise ValueError("ratings_fetched_at is required with ratings")
        ensure_run(
            connection, run_id=data.run_id, season=data.season, windows=data.windows, now=now
        )
        report.tables["team_ratings_snapshot"] = _load_snapshot(
            connection, crosswalk, data.run_id, data.ratings, data.ratings_fetched_at
        )
    return report


# --- teams ---


def _load_teams(
    connection: Connection,
    canonical: Sequence[CanonicalTeam] | None,
    espn_week: Sequence[EspnGame] | None,
) -> TableReport:
    before = _team_rows(connection)
    if canonical is None and not before:
        raise LoadError("`teams` is empty: run `cbb-digest fetch`, which fetches CBBD's teams")
    if canonical is not None:
        _store_teams(connection, canonical)
    if espn_week is not None:
        _store_espn_names(connection, espn_week)
    after = _team_rows(connection)
    return TableReport(changed=sum(1 for slug, row in after.items() if before.get(slug) != row))


def stored_cbbd_teams(connection: Connection) -> list[CbbdTeam]:
    """The stored teams as CBBD served them, to rebuild the canonical teams with the current
    overrides when no teams response was fetched. A row with no ESPN id is left out: only a team
    CBBD no longer lists can lack one."""
    rows = connection.execute(
        select(teams.c.cbbd_id, teams.c.cbbd_name, teams.c.espn_id).where(
            teams.c.espn_id.is_not(None)
        )
    ).all()
    return [
        CbbdTeam(
            id=row.cbbd_id, school=row.cbbd_name, display_name=row.cbbd_name, espn_id=row.espn_id
        )
        for row in rows
    ]


def _team_rows(connection: Connection) -> dict[str, tuple[Any, ...]]:
    return {row.slug: tuple(row) for row in connection.execute(select(teams))}


def _store_teams(connection: Connection, canonical: Sequence[CanonicalTeam]) -> None:
    rows = [dict(row) for row in connection.execute(select(teams)).mappings()]
    slug_by_cbbd_id = {row["cbbd_id"]: row["slug"] for row in rows}
    cbbd_id_by_slug = {row["slug"]: row["cbbd_id"] for row in rows}

    targets: list[dict[str, Any]] = []
    for team in canonical:
        slug = slug_by_cbbd_id.get(team.cbbd_id, team.slug)
        holder = cbbd_id_by_slug.get(slug)
        if holder is not None and holder != team.cbbd_id:
            raise LoadError(
                f"slug {slug!r} for CBBD team {team.cbbd_id} ({team.cbbd_name}) is already "
                f"held by CBBD team {holder}"
            )
        targets.append(
            {
                "slug": slug,
                "cbbd_id": team.cbbd_id,
                "cbbd_name": team.cbbd_name,
                "torvik_name": team.torvik_name,
                "espn_id": team.espn_id,
            }
        )

    # Clear a unique value from the team that loses it before giving it to the one that gains it.
    for column in ("torvik_name", "espn_id"):
        owner = {target[column]: target["slug"] for target in targets}
        for row in rows:
            value = row[column]
            if value is not None and owner.get(value, row["slug"]) != row["slug"]:
                connection.execute(
                    update(teams).where(teams.c.slug == row["slug"]).values({column: None})
                )

    _upsert(
        connection, teams, targets, ["slug"], ["cbbd_id", "cbbd_name", "torvik_name", "espn_id"]
    )


def _store_espn_names(connection: Connection, espn_week: Sequence[EspnGame]) -> None:
    names: dict[str, str] = {}
    for game in espn_week:
        for side in (game.home, game.away):
            if side.is_division_one and side.name is not None:
                names[side.espn_id] = side.name
    for espn_id, name in sorted(names.items()):
        connection.execute(
            update(teams)
            .where(teams.c.espn_id == espn_id, teams.c.espn_name.is_distinct_from(name))
            .values(espn_name=name)
        )


def _crosswalk(connection: Connection) -> Crosswalk:
    """The crosswalk as stored, so every slug it returns is a row of `teams`."""
    rows = connection.execute(
        select(teams.c.slug, teams.c.cbbd_id, teams.c.torvik_name, teams.c.espn_id)
    ).all()
    return Crosswalk(
        by_cbbd_id={row.cbbd_id: row.slug for row in rows},
        by_torvik_name={row.torvik_name: row.slug for row in rows if row.torvik_name is not None},
        by_espn_id={row.espn_id: row.slug for row in rows if row.espn_id is not None},
    )


# --- Torvik ---


def _load_players(
    connection: Connection,
    crosswalk: Crosswalk,
    season: int,
    player_season: Iterable[TorvikPlayerSeason],
) -> TableReport:
    report = TableReport()
    rows: dict[int, dict[str, Any]] = {}
    unmapped: set[str] = set()
    for player in player_season:
        # Before a season starts the file can still hold last season's rows (spec §6).
        if player.season != season:
            report.notes[OTHER_SEASON] += 1
            continue
        slug = crosswalk.slug_for_torvik(player.team)
        if slug is None:
            report.notes[UNMAPPED_TEAM] += 1
            unmapped.add(player.team)
            continue
        if player.pid in rows:
            report.notes[DUPLICATE] += 1
        rows[player.pid] = {
            "season": season,
            "torvik_pid": player.pid,
            "name": player.name,
            "normalized_name": normalise_name(player.name),
            "team_slug": slug,
            "class_year": player.class_year,
            # Torvik serves these as per-game averages; season totals = average * games (§6).
            "games": player.games,
            "min": round(player.min_per_game * player.games, 1),
            "pts": round(player.pts_per_game * player.games),
            "reb": round(player.reb_per_game * player.games),
            "ast": round(player.ast_per_game * player.games),
            "fga": player.fga,
            "fta": player.fta,
        }
    _log_unmapped("torvik player season", unmapped)
    report.changed = _upsert(
        connection, players, list(rows.values()), ["season", "torvik_pid"], _values_of(players)
    )
    return report


def _load_player_games(
    connection: Connection,
    crosswalk: Crosswalk,
    season: int,
    player_games_rows: Iterable[TorvikPlayerGame],
) -> TableReport:
    report = TableReport()
    known = set(
        connection.execute(select(players.c.torvik_pid).where(players.c.season == season)).scalars()
    )
    rows: dict[tuple[int, Any, str], dict[str, Any]] = {}
    unmapped: set[str] = set()
    for game in player_games_rows:
        if crosswalk.slug_for_torvik(game.team) is None:
            report.notes[UNMAPPED_TEAM] += 1
            unmapped.add(game.team)
            continue
        opponent = crosswalk.slug_for_torvik(game.opponent)
        if opponent is None:
            report.notes[UNMAPPED_OPPONENT] += 1
            unmapped.add(game.opponent)
            continue
        if game.pid not in known:
            report.notes[NO_PLAYER] += 1
            continue
        key = (game.pid, game.game_date, opponent)
        if key in rows:
            report.notes[DUPLICATE] += 1
        rows[key] = {
            "season": season,
            "torvik_pid": game.pid,
            "game_date": game.game_date,
            "opponent_slug": opponent,
            "min": game.min,
            "pts": game.pts,
            "reb": game.reb,
            "ast": game.ast,
            "fgm": game.fgm,
            "fga": game.fga,
            "fg3m": game.fg3m,
            "fg3a": game.fg3a,
            "fta": game.fta,
        }
    _log_unmapped("torvik game logs", unmapped)
    report.changed = _upsert(
        connection,
        player_games,
        list(rows.values()),
        ["season", "torvik_pid", "game_date", "opponent_slug"],
        _values_of(player_games),
    )
    return report


# --- ESPN ---


def _load_games(
    connection: Connection, crosswalk: Crosswalk, season: int, espn_week: Iterable[EspnGame]
) -> TableReport:
    report = TableReport()
    rows: dict[str, dict[str, Any]] = {}
    unmapped: set[str] = set()

    def slug(side: EspnTeam) -> str | None:
        found = crosswalk.slug_for_espn(side.espn_id)
        # A non-D-I team is never in the crosswalk, and is not an unmapped team (§7.5).
        if found is None and side.is_division_one:
            report.notes[UNMAPPED_SIDE] += 1
            unmapped.add(f"{side.name} ({side.espn_id})")
        return found

    for game in espn_week:
        if game.espn_id in rows:
            report.notes[DUPLICATE] += 1
        rows[game.espn_id] = {
            "espn_id": game.espn_id,
            "season": season,
            "start_utc": game.start,
            "home_slug": slug(game.home),
            "away_slug": slug(game.away),
            "networks": game.networks,
            "venue": game.venue,
        }
    _log_unmapped("espn week", unmapped)
    report.changed = _upsert(connection, games, list(rows.values()), ["espn_id"], _values_of(games))
    return report


# --- CBBD ratings ---


def ensure_run(
    connection: Connection, *, run_id: str, season: int, windows: Windows, now: datetime
) -> None:
    """Insert a placeholder `runs` row (`status=running`) for `run_id` if it has none, so rows
    that reference the run can be written. An existing row is never changed."""
    connection.execute(
        sqlite_insert(runs)
        .values(
            run_id=run_id,
            kind="digest",
            season=season,
            last_week_start=windows.last_week.start,
            last_week_end=windows.last_week.end,
            this_week_start=windows.this_week.start,
            this_week_end=windows.this_week.end,
            status="running",
            started_at=now,
        )
        .on_conflict_do_nothing(index_elements=[runs.c.run_id])
    )


def _load_snapshot(
    connection: Connection,
    crosswalk: Crosswalk,
    run_id: str,
    ratings: list[CbbdRating],
    fetched_at: datetime,
) -> TableReport:
    """Replace `run_id`'s snapshot with `ratings`, ranked over every team CBBD rated (§3)."""
    report = TableReport()
    new: dict[str, dict[str, Any]] = {}
    for ranked in rank_ratings(ratings):
        slug = crosswalk.slug_for_cbbd(ranked.rating.team_id)
        if slug is None:
            report.notes[UNMAPPED_TEAM] += 1
            continue
        new[slug] = {
            "run_id": run_id,
            "slug": slug,
            "rank": ranked.rank,
            "net": ranked.rating.net,
            "adj_o": ranked.rating.adj_o,
            "adj_d": ranked.rating.adj_d,
            "fetched_at": fetched_at,
        }
    where = team_ratings_snapshot.c.run_id == run_id
    old = {
        row["slug"]: dict(row)
        for row in connection.execute(select(team_ratings_snapshot).where(where)).mappings()
    }
    report.changed = sum(1 for slug in new.keys() | old.keys() if old.get(slug) != new.get(slug))
    if report.changed:
        connection.execute(delete(team_ratings_snapshot).where(where))
        if new:
            connection.execute(insert(team_ratings_snapshot), list(new.values()))
    return report


# --- helpers ---


def _values_of(table: Table) -> list[str]:
    """Every column of `table` that is not part of its primary key."""
    return [column.name for column in table.columns if not column.primary_key]


def _upsert(
    connection: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    values: Sequence[str],
) -> int:
    """Insert `rows`, or update `values` on a key conflict where one of them differs.

    Returns the rows inserted or changed: SQLite counts no change for a conflict whose `WHERE`
    is false, so an unchanged row is not counted.
    """
    if not rows:
        return 0
    statement = sqlite_insert(table)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c[name] for name in key],
        set_={name: statement.excluded[name] for name in values},
        where=or_(*(table.c[name].is_distinct_from(statement.excluded[name]) for name in values)),
    )
    return connection.execute(statement, list(rows)).rowcount


def _log_unmapped(source: str, names: set[str]) -> None:
    if names:
        log.warning("unmapped teams dropped", extra={"source": source, "teams": sorted(names)})
