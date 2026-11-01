"""Watchlist last week: per-player lines, TS%, the notable flag and the inactive alert (spec §5,
§8.4, §7.4)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert, select

from cbb_digest import db
from cbb_digest.compute.models import Tier, WatchlistWeekSection
from cbb_digest.compute.watchlist_week import (
    INACTIVE_DETAIL,
    MAX_ROWS,
    true_shooting,
    watchlist_last_week,
)
from cbb_digest.resolve import Alert, resolve
from cbb_digest.runs import start_run
from cbb_digest.schema import player_games, players, teams, watchlist_resolution
from cbb_digest.watchlist import Watchlist, WatchlistPlayer, normalise_name
from cbb_digest.weeks import run_id_for, windows_for

SEASON = 2027
# Last week for this Monday is Mon 2026-11-09 through Sun 2026-11-15.
MONDAY = date(2026, 11, 16)
LAST_WEEK = date(2026, 11, 10)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
SLUGS = ("t01", "t02", "t03", "opp")


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


class _World:
    """Builds the stored state `watchlist_last_week` reads: players, game logs, resolutions."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.entries: list[WatchlistPlayer] = []
        self._next_pid = 1

    def player(self, team: str, *, tier: Tier | None = None, name: str | None = None) -> int:
        """A player in Torvik's file on `team`; on the watchlist when `tier` is given."""
        pid = self._next_pid
        self._next_pid += 1
        name = name or f"Player {pid}"
        self.connection.execute(
            insert(players).values(
                season=SEASON,
                torvik_pid=pid,
                name=name,
                normalized_name=normalise_name(name),
                team_slug=team,
                class_year="So",
                games=1,
                min=30.0,
                pts=10,
                reb=5,
                ast=3,
                fga=8,
                fta=2,
            )
        )
        if tier is not None:
            self.entries.append(
                WatchlistPlayer.model_validate(
                    {"name": name, "team": f"Team {team.upper()}", "tier": tier, "class": SEASON}
                )
            )
        return pid

    def game(
        self,
        pid: int,
        day: date,
        *,
        minutes: float = 30.0,
        pts: int = 10,
        reb: int = 5,
        ast: int = 3,
        fgm: int = 4,
        fga: int = 8,
        fg3m: int = 1,
        fg3a: int = 3,
        fta: int = 2,
        opponent: str = "opp",
    ) -> None:
        self.connection.execute(
            insert(player_games).values(
                season=SEASON,
                torvik_pid=pid,
                game_date=day,
                opponent_slug=opponent,
                min=minutes,
                pts=pts,
                reb=reb,
                ast=ast,
                fgm=fgm,
                fga=fga,
                fg3m=fg3m,
                fg3a=fg3a,
                fta=fta,
            )
        )

    def week(self, monday: date = MONDAY) -> WatchlistWeekSection:
        """Start the run for `monday`, resolve the watchlist and compute the section."""
        run_id = run_id_for(monday, SEASON)
        windows = windows_for(monday)
        now = datetime.combine(monday, datetime.min.time(), UTC) + timedelta(hours=12)
        start_run(
            self.connection,
            run_id=run_id,
            kind="digest",
            season=SEASON,
            windows=windows,
            image_sha=None,
            now=now,
        )
        watchlist = Watchlist(season=SEASON, players=tuple(self.entries))
        resolve(self.connection, watchlist, run_id=run_id, windows=windows, now=now)
        return watchlist_last_week(self.connection, run_id, watchlist)


def _notables(section: WatchlistWeekSection) -> dict[str, str | None]:
    return {row.name: row.notable for row in section.rows}


# --- DNP and inactive ---


def test_dnp_then_a_2nd_straight_week_raises_inactive_once(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        prospect = world.player("t01", tier=1, name="Sitting Out")
        teammate = world.player("t01")
        world.game(prospect, date(2026, 11, 3))
        for day in (LAST_WEEK, date(2026, 11, 17), date(2026, 11, 24)):
            world.game(teammate, day)

        first = world.week()
        second = world.week(MONDAY + timedelta(days=7))
        third = world.week(MONDAY + timedelta(days=14))
        rerun_of_second = world.week(MONDAY + timedelta(days=7))

    assert _notables(first) == {"Sitting Out": "DNP"}
    assert first.rows[0].games == 0
    assert first.rows[0].minutes_per_game == 0.0
    assert first.rows[0].true_shooting is None
    assert first.alerts == []

    inactive = Alert("inactive", "Sitting Out", INACTIVE_DETAIL)
    assert _notables(second) == {"Sitting Out": "DNP — 2nd straight week"}
    assert second.alerts == [inactive]
    assert _notables(third) == {"Sitting Out": "DNP — 3rd straight week"}
    assert third.alerts == []
    # A forced re-run of a week shows the alerts that week originally raised (§7.4).
    assert rerun_of_second.alerts == [inactive]


def test_playing_again_clears_inactive_so_a_later_spell_raises_it_again(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        prospect = world.player("t01", tier=1, name="Back And Forth")
        # The team's games show up through its opponents' rows, too.
        opponent = world.player("t02")
        for day in (date(2026, 11, 3), LAST_WEEK, date(2026, 11, 24), date(2026, 12, 1)):
            world.game(opponent, day, opponent="t01")
        world.game(prospect, date(2026, 11, 17))

        raised = world.week()
        played = world.week(MONDAY + timedelta(days=7))
        sat_again = world.week(MONDAY + timedelta(days=14))
        inactive_again = world.week(MONDAY + timedelta(days=21))
        stored = connection.execute(select(watchlist_resolution.c.inactive_run_id)).scalar_one()

    assert _notables(raised) == {"Back And Forth": "DNP — 2nd straight week"}
    assert len(raised.alerts) == 1
    assert _notables(played) == {"Back And Forth": None}
    assert _notables(sat_again) == {"Back And Forth": "DNP"}
    assert _notables(inactive_again) == {"Back And Forth": "DNP — 2nd straight week"}
    assert len(inactive_again.alerts) == 1
    assert stored == run_id_for(MONDAY + timedelta(days=21), SEASON)


def test_a_break_inside_a_dnp_spell_does_not_raise_inactive_again(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player("t01", tier=1, name="Long Spell")
        teammate = world.player("t01")
        # The team plays in weeks 1 and 2, has a week off (exams), then plays weeks 4 and 5.
        for day in (date(2026, 11, 3), LAST_WEEK, date(2026, 11, 24), date(2026, 12, 1)):
            world.game(teammate, day)

        raised = world.week()
        sections = [world.week(MONDAY + timedelta(days=7 * n)) for n in (1, 2, 3)]

    assert len(raised.alerts) == 1
    after_break, back, still_out = sections
    assert after_break.rows == []
    assert _notables(back) == {"Long Spell": "DNP"}
    assert _notables(still_out) == {"Long Spell": "DNP — 2nd straight week"}
    assert [section.alerts for section in sections] == [[], [], []]


def test_an_entry_added_mid_spell_shares_the_alert_a_re_run_shows(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player("t01", tier=2, name="Late Add")
        teammate = world.player("t01")
        for day in (date(2026, 11, 3), LAST_WEEK, date(2026, 11, 17)):
            world.game(teammate, day)

        raised = world.week()
        world.entries.append(
            WatchlistPlayer.model_validate(
                {"name": "Late Add", "team": None, "tier": 1, "class": SEASON}
            )
        )
        later = world.week(MONDAY + timedelta(days=7))
        rerun = world.week()

    assert len(raised.alerts) == 1
    assert later.alerts == []
    assert rerun.alerts == raised.alerts


def test_a_player_whose_team_did_not_play_gets_no_row(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        idle = world.player("t01", tier=1)
        world.game(idle, date(2026, 11, 3))
        busy = world.player("t02", tier=1, name="Busy")
        world.game(busy, LAST_WEEK)

        section = world.week()

    assert [row.name for row in section.rows] == ["Busy"]


# --- season high ---


def test_31_points_after_4_lower_games_is_a_season_high(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        scorer = world.player("t01", tier=1, name="Scorer")
        for day, pts in zip(range(1, 5), (12, 20, 30, 18), strict=True):
            world.game(scorer, date(2026, 11, day), pts=pts)
        world.game(scorer, LAST_WEEK, pts=31)

        section = world.week()

    assert _notables(section) == {"Scorer": "Season high: 31 pts"}


def test_a_high_in_game_2_or_one_that_only_ties_is_no_flag(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        early = world.player("t01", tier=1, name="Early")
        world.game(early, date(2026, 11, 3), pts=10)
        world.game(early, LAST_WEEK, pts=25)
        tied = world.player("t02", tier=1, name="Tied")
        for day, pts in zip(range(1, 5), (12, 31, 18, 9), strict=True):
            world.game(tied, date(2026, 11, day), pts=pts)
        world.game(tied, LAST_WEEK, pts=31)

        section = world.week()

    assert _notables(section) == {"Early": None, "Tied": None}


# --- 0-for-N ---


def test_0_for_6_from_three_beats_0_for_5_from_the_field(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        cold = world.player("t01", tier=1, name="Cold")
        world.game(cold, LAST_WEEK, fgm=3, fga=10, fg3m=0, fg3a=6)
        world.game(cold, LAST_WEEK + timedelta(days=2), fgm=0, fga=5, fg3m=0, fg3a=2, pts=2)

        section = world.week()

    assert _notables(section) == {"Cold": "0-for-6 3P"}


def test_a_season_high_outranks_0_for_n(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        both = world.player("t01", tier=1, name="Both")
        for day in range(1, 4):
            world.game(both, date(2026, 11, day), pts=4)
        world.game(both, LAST_WEEK, pts=9, fgm=0, fga=7, fg3m=0, fg3a=4, fta=10)

        section = world.week()

    assert _notables(section) == {"Both": "Season high: 9 pts"}


# --- the line ---


def test_ts_for_20_pts_on_14_fga_and_5_fta_is_62_percent(engine: Engine) -> None:
    assert true_shooting(0, 0, 0) is None
    ts = true_shooting(20, 14, 5)
    assert ts is not None and round(ts * 100) == 62

    with engine.begin() as connection:
        world = _World(connection)
        shooter = world.player("t01", tier=2, name="Shooter")
        world.game(shooter, LAST_WEEK, minutes=32, pts=12, reb=6, ast=2, fga=9, fg3a=4, fta=3)
        world.game(
            shooter,
            LAST_WEEK + timedelta(days=3),
            minutes=28,
            pts=8,
            reb=4,
            ast=4,
            fga=5,
            fg3a=1,
            fta=2,
        )

        section = world.week()

    [row] = section.rows
    assert row.team_name == "Team T01"
    assert row.tier == 2
    assert row.games == 2
    assert row.minutes_per_game == 30.0
    assert row.points_per_game == 10.0
    assert row.rebounds_per_game == 5.0
    assert row.assists_per_game == 3.0
    assert row.threes_attempted_per_game == 2.5
    assert row.true_shooting is not None and round(row.true_shooting * 100) == 62


def test_rows_sort_by_tier_then_minutes_and_cap_at_40(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        expected: list[tuple[int, float]] = []
        for n in range(45):
            tier: Tier = (3, 1, 2)[n % 3]
            minutes = float(10 + n)
            pid = world.player(SLUGS[n % 3], tier=tier)
            world.game(pid, LAST_WEEK, minutes=minutes)
            expected.append((tier, minutes))

        section = world.week()

    expected.sort(key=lambda item: (item[0], -item[1]))
    assert [(row.tier, row.minutes_per_game) for row in section.rows] == expected[:MAX_ROWS]
    assert section.more_count == 45 - MAX_ROWS


def test_a_player_on_two_entries_gets_one_row_at_the_best_tier(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        pid = world.player("t01", tier=3, name="Twice")
        world.entries.append(
            WatchlistPlayer.model_validate(
                {"name": "Twice", "team": None, "tier": 1, "class": SEASON}
            )
        )
        world.game(pid, LAST_WEEK)

        section = world.week()

    assert [(row.name, row.tier) for row in section.rows] == [("Twice", 1)]
