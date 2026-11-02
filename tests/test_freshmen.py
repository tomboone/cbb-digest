"""Freshmen to watch: the top-5 freshmen by minutes on top-100 teams (spec §5, §8.5)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert

from cbb_digest import db
from cbb_digest.compute.freshmen import MAX_RANK, MAX_ROWS, freshmen_to_watch
from cbb_digest.compute.models import FreshmanRow, FreshmenSection
from cbb_digest.resolve import resolve
from cbb_digest.runs import start_run
from cbb_digest.schema import players, team_ratings_snapshot, teams
from cbb_digest.watchlist import Watchlist, WatchlistPlayer, normalise_name
from cbb_digest.weeks import run_id_for, windows_for

SEASON = 2027
MONDAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
RUN_ID = run_id_for(MONDAY, SEASON)
# Slug → this run's rank; "none" is missing from the snapshot.
RANKS = {"top": 4, "edge": 100, "low": 150}
SLUGS = (*RANKS, "none")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            insert(teams),
            [
                {"slug": slug, "cbbd_id": n, "cbbd_name": f"Team {slug.title()}"}
                for n, slug in enumerate(SLUGS, start=1)
            ],
        )
    yield engine
    engine.dispose()


class _World:
    """Builds the stored state `freshmen_to_watch` reads: the run, its snapshot, players."""

    def __init__(self, connection: Connection, *, snapshot: bool = True) -> None:
        self.connection = connection
        self.entries: list[WatchlistPlayer] = []
        self._next_pid = 1
        start_run(
            connection,
            run_id=RUN_ID,
            kind="digest",
            season=SEASON,
            windows=windows_for(MONDAY),
            image_sha=None,
            now=NOW,
        )
        if snapshot:
            connection.execute(
                insert(team_ratings_snapshot),
                [
                    {
                        "run_id": RUN_ID,
                        "slug": slug,
                        "rank": rank,
                        "net": 0.0,
                        "adj_o": 110.0,
                        "adj_d": 100.0,
                        "fetched_at": NOW,
                    }
                    for slug, rank in RANKS.items()
                ],
            )

    def player(
        self,
        team: str = "top",
        *,
        minutes: float = 300.0,
        class_year: str | None = "Fr",
        name: str | None = None,
        watchlist: bool = False,
        season: int = SEASON,
        games: int = 10,
        pts: int = 100,
        reb: int = 50,
        ast: int = 30,
        fga: int = 80,
        fta: int = 20,
    ) -> str:
        pid = self._next_pid
        self._next_pid += 1
        name = name or f"Player {pid:02d}"
        self.connection.execute(
            insert(players).values(
                season=season,
                torvik_pid=pid,
                name=name,
                normalized_name=normalise_name(name),
                team_slug=team,
                class_year=class_year,
                games=games,
                min=minutes,
                pts=pts,
                reb=reb,
                ast=ast,
                fga=fga,
                fta=fta,
            )
        )
        if watchlist:
            self.entries.append(
                WatchlistPlayer.model_validate(
                    {"name": name, "team": f"Team {team.title()}", "tier": 2, "class": SEASON}
                )
            )
        return name

    def section(self) -> FreshmenSection:
        watchlist = Watchlist(season=SEASON, players=tuple(self.entries))
        resolve(self.connection, watchlist, run_id=RUN_ID, windows=windows_for(MONDAY), now=NOW)
        return freshmen_to_watch(self.connection, RUN_ID, watchlist)


def _names(section: FreshmenSection) -> list[str]:
    return [row.name for row in section.rows]


def test_takes_the_five_freshmen_with_the_most_minutes(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        by_minutes = [world.player(minutes=100.0 + 10 * n) for n in range(7)]
        section = world.section()
    assert MAX_ROWS == 5
    assert _names(section) == by_minutes[::-1][:MAX_ROWS]


def test_a_freshman_on_a_team_ranked_below_100_is_excluded_however_many_minutes(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player("low", minutes=900.0)
        edge = world.player("edge", minutes=200.0)
        top = world.player("top", minutes=100.0)
        section = world.section()
    assert MAX_RANK == 100
    assert _names(section) == [edge, top]


def test_a_freshman_whose_team_is_missing_from_the_snapshot_is_excluded(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player("none", minutes=900.0)
        top = world.player("top")
        section = world.section()
    assert _names(section) == [top]


def test_a_resolved_watchlist_freshman_is_excluded(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player(minutes=900.0, name="Listed Freshman", watchlist=True)
        other = world.player()
        section = world.section()
    assert _names(section) == [other]


def test_only_freshmen_qualify(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        for class_year in ("So", "Jr", "Sr", None):
            world.player(minutes=900.0, class_year=class_year)
        freshman = world.player()
        section = world.section()
    assert _names(section) == [freshman]


def test_players_from_another_season_are_excluded(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player(minutes=900.0, season=SEASON - 1)
        freshman = world.player()
        section = world.section()
    assert _names(section) == [freshman]


def test_ties_on_minutes_keep_a_stable_order(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        for name in ("Cole Ames", "Abe Zane", "Ben Young"):
            world.player(minutes=300.0, name=name)
        world.player(minutes=400.0, name="Zed Most")
        section = world.section()
    assert _names(section) == ["Zed Most", "Abe Zane", "Ben Young", "Cole Ames"]


def test_fewer_than_five_candidates_gives_fewer_rows(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        names = [world.player(minutes=300.0 - n) for n in range(2)]
        section = world.section()
    assert _names(section) == names


def test_no_candidates_gives_an_empty_section(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player(class_year="Sr")
        section = world.section()
    assert section.rows == []


def test_a_row_carries_team_rank_and_per_game_figures(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player(
            "edge",
            name="Ace Freshman",
            games=10,
            minutes=285.0,
            pts=150,
            reb=62,
            ast=27,
            fga=110,
            fta=25,
        )
        section = world.section()
    assert section.rows == [
        FreshmanRow(
            name="Ace Freshman",
            torvik_pid=1,
            team_slug="edge",
            team_name="Team Edge",
            team_rank=100,
            games=10,
            minutes_per_game=28.5,
            points_per_game=15.0,
            rebounds_per_game=6.2,
            assists_per_game=2.7,
            true_shooting=150 / (2 * (110 + 0.44 * 25)),
        )
    ]


def test_true_shooting_is_none_without_attempts(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.player(pts=0, fga=0, fta=0)
        section = world.section()
    assert section.rows[0].true_shooting is None


def test_a_run_with_no_snapshot_is_an_error(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection, snapshot=False)
        world.player()
        with pytest.raises(LookupError):
            world.section()


def test_an_unknown_run_is_an_error(engine: Engine) -> None:
    with engine.begin() as connection:
        watchlist = Watchlist(season=SEASON, players=())
        with pytest.raises(LookupError):
            freshmen_to_watch(connection, "2027-W99", watchlist)
