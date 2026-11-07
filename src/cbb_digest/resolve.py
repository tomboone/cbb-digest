"""`cbb-digest resolve`: each watchlist player tied to one Torvik player, or told why not (spec
§7.3), and an alert only when that answer changes (§7.4).

Resolution reads `players` for the watchlist's season and tries, in order:

1. **Exact:** the normalised name on the stated team matches one row → `resolved`.
2. **Team changed:** the normalised name matches one row on any team → `resolved`, with a
   `team_changed` alert when the entry states a team (with no stated team there is nothing to
   have changed, so no alert).
3. **Fuzzy:** the entry states a team, and `token_sort_ratio` ≥ 90 against that team's names
   gives one row → `resolved` with `fuzzy_match`.
4. Otherwise `ambiguous` when any step found more than one row, else `unresolved`. Nothing is
   ever guessed.

The stated team maps through `teams` by an exact, case-blind match on its slug or on CBBD's,
Torvik's or ESPN's name for it, never by similarity (§7.2). A team that maps to no slug, or to
more than one, makes the entry `unresolved` with an `unmapped_team` alert, and no matching is
tried: the stated team is part of the entry's identity, and a row holds one alert, the one the
reader can act on by fixing `watchlist.yaml` or `teams_crosswalk.yaml`. The entry is stored under
`unmapped_watchlist_key`, so its alert, like any other, is raised once.

**Alerts on change.** Each entry's answer is upserted into `watchlist_resolution`.
`changed_run_id` moves to this run only when the outcome, Torvik id, team, alert kind or alert
detail differs from what is stored; `first_run_id` moves only when the outcome does. A respelled
match updates `matched_name` without counting as a change. The alerts returned are those whose
row was changed by this run, so a second run over the same data returns none, and a forced
re-run of the same week returns that week's alerts again.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from rapidfuzz import fuzz
from sqlalchemy import Connection, exists, insert, select, update

from cbb_digest.runs import ensure_run
from cbb_digest.schema import player_games, players, teams, watchlist_resolution
from cbb_digest.watchlist import (
    Watchlist,
    WatchlistPlayer,
    normalise_name,
    unmapped_watchlist_key,
    watchlist_key,
)
from cbb_digest.weeks import Windows

FUZZY_THRESHOLD = 90

type Outcome = Literal["resolved", "unresolved", "ambiguous"]
type ResolutionAlertKind = Literal[
    "unresolved", "ambiguous", "team_changed", "fuzzy_match", "unmapped_team"
]
# `inactive` is raised by the compute step from the game logs, not by resolution (§7.4).
type AlertKind = Literal[
    "unresolved", "ambiguous", "team_changed", "fuzzy_match", "unmapped_team", "inactive"
]

NOT_IN_FILE = "not in Torvik's file"
NOT_YET_PLAYED = "not yet in Torvik's file (team has not played)"


class ResolveError(RuntimeError):
    """Resolution cannot start, e.g. there are no teams to map the stated teams with."""


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    # The watchlist player's name as written in `watchlist.yaml`.
    name: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.name} ({self.detail})"


@dataclass(frozen=True, slots=True)
class Resolution:
    """One entry's answer, as stored in `watchlist_resolution`."""

    outcome: Outcome
    torvik_pid: int | None = None
    team_slug: str | None = None
    matched_name: str | None = None
    alert_kind: ResolutionAlertKind | None = None
    alert_detail: str | None = None


@dataclass(frozen=True, slots=True)
class Entry:
    """One watchlist player's line in the `resolve` table."""

    player: WatchlistPlayer
    key: str
    resolution: Resolution
    # The entry's standing alert, if any, whether or not this run raised it.
    alert: Alert | None
    # Whether this run raised `alert`.
    new: bool


@dataclass(frozen=True, slots=True)
class ResolveReport:
    entries: list[Entry]
    # The alerts this run raised, in watchlist order.
    alerts: list[Alert]


@dataclass(frozen=True, slots=True)
class ResolvedPlayer:
    """A watchlist player whose stored resolution is `resolved`, as the compute step reads it."""

    player: WatchlistPlayer
    torvik_pid: int
    # The player's current team, which may differ from the one the entry states.
    team_slug: str
    # Torvik's spelling of the name, which the player's Torvik link needs (§5).
    torvik_name: str


@dataclass(frozen=True, slots=True)
class _Player:
    pid: int
    name: str
    normalized_name: str
    team_slug: str


@dataclass(frozen=True, slots=True)
class _Teams:
    # Casefolded slug or source name → the slugs it names.
    slugs_by_name: Mapping[str, frozenset[str]]
    display: Mapping[str, str]

    def slugs_for(self, stated: str) -> frozenset[str]:
        return self.slugs_by_name.get(stated.strip().casefold(), frozenset())

    def name_of(self, slug: str) -> str:
        return self.display.get(slug, slug)


def resolve(
    connection: Connection,
    watchlist: Watchlist,
    *,
    run_id: str,
    windows: Windows,
    now: datetime,
) -> ResolveReport:
    """Resolve every entry of `watchlist` and store the answers for `run_id`. Run it inside one
    transaction. Raises `ResolveError` when `teams` is empty."""
    team_index = _team_index(connection)
    season = watchlist.season
    roster = _players(connection, season)
    by_name: dict[str, list[_Player]] = defaultdict(list)
    by_team: dict[str, list[_Player]] = defaultdict(list)
    for player in roster:
        by_name[player.normalized_name].append(player)
        by_team[player.team_slug].append(player)
    played = _teams_that_played(connection, season)
    ensure_run(connection, run_id=run_id, season=season, windows=windows, now=now)

    entries: list[Entry] = []
    for player in watchlist.players:
        mapped, slugs = _stated_team(player, team_index)
        if not mapped:
            assert player.team is not None
            key = unmapped_watchlist_key(player)
            resolution = _unmapped(player.team, slugs, team_index)
        else:
            slug = next(iter(slugs), None)
            key = watchlist_key(player, slug)
            resolution = _resolve_one(player, slug, by_name, by_team, played, team_index)
        new = _store(connection, season, key, resolution, run_id)
        alert = (
            Alert(resolution.alert_kind, player.name, resolution.alert_detail or "")
            if resolution.alert_kind is not None
            else None
        )
        entries.append(Entry(player, key, resolution, alert, new=new and alert is not None))
    return ResolveReport(
        entries=entries,
        alerts=[entry.alert for entry in entries if entry.new and entry.alert is not None],
    )


def resolved_players(connection: Connection, watchlist: Watchlist) -> list[ResolvedPlayer]:
    """The watchlist players whose stored resolution is `resolved`, in watchlist order.

    Reads what the last `resolve` stored and resolves nothing itself. Each entry is keyed the way
    `resolve` keys it, through the same crosswalk, so an entry whose stated team does not map is
    never among them: it is stored as `unresolved` under its `unmapped_watchlist_key`. Raises
    `ResolveError` when `teams` is empty.
    """
    team_index = _team_index(connection)
    query = select(
        watchlist_resolution.c.watchlist_key,
        watchlist_resolution.c.torvik_pid,
        watchlist_resolution.c.team_slug,
        watchlist_resolution.c.matched_name,
    ).where(
        watchlist_resolution.c.season == watchlist.season,
        watchlist_resolution.c.outcome == "resolved",
    )
    stored = {row.watchlist_key: row for row in connection.execute(query)}

    found: list[ResolvedPlayer] = []
    for player in watchlist.players:
        mapped, slugs = _stated_team(player, team_index)
        if not mapped:
            continue
        row = stored.get(watchlist_key(player, next(iter(slugs), None)))
        if row is None:
            continue
        found.append(ResolvedPlayer(player, row.torvik_pid, row.team_slug, row.matched_name))
    return found


def raised_alerts(connection: Connection, watchlist: Watchlist, run_id: str) -> list[Alert]:
    """The resolution alerts `run_id` raised, in watchlist order: those of the entries whose row
    it last changed, as `resolve` returned them (§7.4).

    Read back from `watchlist_resolution`, so the compute step can list them without resolving
    again, and a run that skipped resolution because Torvik failed (§9.3) lists none of its own.
    Raises `ResolveError` when `teams` is empty.
    """
    team_index = _team_index(connection)
    query = select(
        watchlist_resolution.c.watchlist_key,
        watchlist_resolution.c.alert_kind,
        watchlist_resolution.c.alert_detail,
    ).where(
        watchlist_resolution.c.season == watchlist.season,
        watchlist_resolution.c.changed_run_id == run_id,
        watchlist_resolution.c.alert_kind.is_not(None),
    )
    stored = {row.watchlist_key: row for row in connection.execute(query)}

    alerts: list[Alert] = []
    for player in watchlist.players:
        mapped, slugs = _stated_team(player, team_index)
        if mapped:
            key = watchlist_key(player, next(iter(slugs), None))
        else:
            key = unmapped_watchlist_key(player)
        row = stored.get(key)
        if row is not None:
            alerts.append(Alert(row.alert_kind, player.name, row.alert_detail or ""))
    return alerts


def _stated_team(player: WatchlistPlayer, team_index: _Teams) -> tuple[bool, frozenset[str]]:
    """Whether the entry's stated team maps, and the slugs it names: none when the entry states
    no team (which counts as mapped), exactly one when it maps. `resolve` and
    `resolved_players` must key an entry alike, so both decide it here."""
    if player.team is None:
        return True, frozenset()
    slugs = team_index.slugs_for(player.team)
    return len(slugs) == 1, slugs


def _resolve_one(
    player: WatchlistPlayer,
    slug: str | None,
    by_name: Mapping[str, Sequence[_Player]],
    by_team: Mapping[str, Sequence[_Player]],
    played: frozenset[str],
    team_index: _Teams,
) -> Resolution:
    name = normalise_name(player.name)
    same_name = by_name.get(name, ())

    # 1. Exact: the name on the stated team.
    if slug is not None:
        on_team = [p for p in same_name if p.team_slug == slug]
        if len(on_team) == 1:
            return _resolved(on_team[0])
        if on_team:
            return _ambiguous(on_team, team_index)

    # 2. Team changed: the name on any team.
    if len(same_name) == 1:
        match = same_name[0]
        if slug is None:
            return _resolved(match)
        detail = f"was {team_index.name_of(slug)}, now {team_index.name_of(match.team_slug)}"
        return _resolved(match, "team_changed", detail)
    if same_name:
        return _ambiguous(same_name, team_index)

    # 3. Fuzzy: a close spelling on the stated team.
    if slug is not None:
        close = [
            p
            for p in by_team.get(slug, ())
            if fuzz.token_sort_ratio(name, p.normalized_name) >= FUZZY_THRESHOLD
        ]
        if len(close) == 1:
            return _resolved(close[0], "fuzzy_match", f'matched "{close[0].name}"')
        if close:
            return _ambiguous(close, team_index)

    # 4. Nothing.
    detail = NOT_YET_PLAYED if slug is not None and slug not in played else NOT_IN_FILE
    return Resolution("unresolved", alert_kind="unresolved", alert_detail=detail)


def _resolved(
    player: _Player, kind: ResolutionAlertKind | None = None, detail: str | None = None
) -> Resolution:
    return Resolution(
        "resolved",
        torvik_pid=player.pid,
        team_slug=player.team_slug,
        matched_name=player.name,
        alert_kind=kind,
        alert_detail=detail,
    )


def _unmapped(stated: str, slugs: frozenset[str], team_index: _Teams) -> Resolution:
    if slugs:
        names = ", ".join(sorted(team_index.name_of(slug) for slug in slugs))
        detail = f'"{stated}" names more than one team: {names}'
    else:
        detail = f'no crosswalk entry for "{stated}"'
    return Resolution("unresolved", alert_kind="unmapped_team", alert_detail=detail)


def _ambiguous(candidates: Sequence[_Player], team_index: _Teams) -> Resolution:
    listed = ", ".join(
        f"{p.name} ({team_index.name_of(p.team_slug)}, {p.pid})"
        for p in sorted(candidates, key=lambda p: (p.name, team_index.name_of(p.team_slug), p.pid))
    )
    return Resolution("ambiguous", alert_kind="ambiguous", alert_detail=f"candidates: {listed}")


def _store(
    connection: Connection, season: int, key: str, resolution: Resolution, run_id: str
) -> bool:
    """Upsert `resolution` for (`season`, `key`); whether its row's `changed_run_id` is now
    `run_id`, either because this run changed it or because an earlier pass of this run did."""
    where = (watchlist_resolution.c.season == season) & (
        watchlist_resolution.c.watchlist_key == key
    )
    stored = connection.execute(select(watchlist_resolution).where(where)).mappings().first()
    values: dict[str, Any] = {
        "outcome": resolution.outcome,
        "torvik_pid": resolution.torvik_pid,
        "team_slug": resolution.team_slug,
        "matched_name": resolution.matched_name,
        "alert_kind": resolution.alert_kind,
        "alert_detail": resolution.alert_detail,
    }
    if stored is None:
        connection.execute(
            insert(watchlist_resolution).values(
                season=season,
                watchlist_key=key,
                first_run_id=run_id,
                changed_run_id=run_id,
                **values,
            )
        )
        return True

    changed = any(
        stored[column] != values[column]
        for column in ("outcome", "torvik_pid", "team_slug", "alert_kind", "alert_detail")
    )
    if changed:
        values["changed_run_id"] = run_id
        if stored["outcome"] != resolution.outcome:
            values["first_run_id"] = run_id
    if changed or stored["matched_name"] != resolution.matched_name:
        connection.execute(update(watchlist_resolution).where(where).values(values))
    return changed or stored["changed_run_id"] == run_id


def _team_index(connection: Connection) -> _Teams:
    rows = connection.execute(
        select(teams.c.slug, teams.c.cbbd_name, teams.c.torvik_name, teams.c.espn_name)
    ).all()
    if not rows:
        raise ResolveError("`teams` is empty: run `cbb-digest fetch` and `cbb-digest load` first")
    slugs: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for name in (row.slug, row.cbbd_name, row.torvik_name, row.espn_name):
            if name is not None:
                slugs[name.strip().casefold()].add(row.slug)
    return _Teams(
        slugs_by_name={name: frozenset(found) for name, found in slugs.items()},
        display={row.slug: row.cbbd_name for row in rows},
    )


def _players(connection: Connection, season: int) -> list[_Player]:
    rows = connection.execute(
        select(players.c.torvik_pid, players.c.name, players.c.normalized_name, players.c.team_slug)
        .where(players.c.season == season)
        .order_by(players.c.torvik_pid)
    ).all()
    return [_Player(row.torvik_pid, row.name, row.normalized_name, row.team_slug) for row in rows]


def _teams_that_played(connection: Connection, season: int) -> frozenset[str]:
    """The slugs with a game in `player_games` this season, on either side."""
    own = select(players.c.team_slug).where(
        players.c.season == season,
        exists().where(
            player_games.c.season == players.c.season,
            player_games.c.torvik_pid == players.c.torvik_pid,
        ),
    )
    opponents = select(player_games.c.opponent_slug).where(player_games.c.season == season)
    slugs = set(connection.execute(own).scalars()) | set(connection.execute(opponents).scalars())
    return frozenset(slugs)
