"""Featured games for this week, in the order the reader picks from (spec §8.3)."""

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, insert

from cbb_digest import db
from cbb_digest.compute.games import featured_games
from cbb_digest.compute.models import FeaturedGamesSection, FeaturedTeam, Tier
from cbb_digest.resolve import resolve
from cbb_digest.runs import start_run
from cbb_digest.schema import games, players, team_ratings_snapshot, teams
from cbb_digest.watchlist import Watchlist, WatchlistPlayer, normalise_name
from cbb_digest.weeks import EASTERN, run_id_for, windows_for

SEASON = 2027
MONDAY = date(2026, 11, 9)
RUN = run_id_for(MONDAY, SEASON)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
# Tuesday 7:00 PM ET, inside this week.
TIP = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)

SLUGS = [f"t{n:02d}" for n in range(1, 31)]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            insert(teams),
            [
                {"slug": slug, "cbbd_id": n, "cbbd_name": _name(slug)}
                for n, slug in enumerate(SLUGS, start=1)
            ],
        )
        start_run(
            connection,
            run_id=RUN,
            kind="digest",
            season=SEASON,
            windows=windows_for(MONDAY),
            image_sha=None,
            now=NOW,
        )
    yield engine
    engine.dispose()


def _name(slug: str) -> str:
    return f"Team {slug.upper()}"


class _World:
    """Builds the stored state `featured_games` reads: players, resolutions, games, ranks."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.entries: list[WatchlistPlayer] = []
        self._next_pid = 1
        self._next_game = 1

    def prospect(
        self, team: str, tier: Tier, *, now_on: str | None = None, name: str | None = None
    ) -> str:
        """A watchlist player stated on `team`, in Torvik's file on `now_on` (default `team`)."""
        pid = self._next_pid
        self._next_pid += 1
        name = name or f"Player {pid}"
        self.connection.execute(
            insert(players).values(
                season=SEASON,
                torvik_pid=pid,
                name=name,
                normalized_name=normalise_name(name),
                team_slug=now_on or team,
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
        self.entries.append(
            WatchlistPlayer.model_validate(
                {"name": name, "team": _name(team), "tier": tier, "class": SEASON}
            )
        )
        return name

    def unresolvable(self, team: str, tier: Tier) -> None:
        """A watchlist player stated on `team` whom Torvik's file does not have."""
        self.entries.append(
            WatchlistPlayer.model_validate(
                {"name": "Nobody Here", "team": _name(team), "tier": tier, "class": SEASON}
            )
        )

    def game(
        self,
        home: str | None,
        away: str | None,
        *,
        away_name: str | None = None,
        start: datetime = TIP,
        networks: Sequence[str] = ("ESPN",),
        venue: str | None = "Arena",
    ) -> str:
        espn_id = f"g{self._next_game}"
        self._next_game += 1
        self.connection.execute(
            insert(games).values(
                espn_id=espn_id,
                season=SEASON,
                start_utc=start,
                home_slug=home,
                away_slug=away,
                home_name=f"ESPN {home}",
                away_name=away_name or f"ESPN {away}",
                networks=list(networks),
                venue=venue,
            )
        )
        return espn_id

    def ranks(self, ranks: Mapping[str, int]) -> None:
        self.connection.execute(
            insert(team_ratings_snapshot),
            [
                {
                    "run_id": RUN,
                    "slug": slug,
                    "rank": rank,
                    "net": 0.0,
                    "adj_o": 100.0,
                    "adj_d": 100.0,
                    "fetched_at": NOW,
                }
                for slug, rank in ranks.items()
            ],
        )

    def featured(self, *, ranked: bool = True) -> FeaturedGamesSection:
        watchlist = Watchlist(season=SEASON, players=tuple(self.entries))
        resolve(self.connection, watchlist, run_id=RUN, windows=windows_for(MONDAY), now=NOW)
        return featured_games(self.connection, RUN, watchlist, ranked=ranked)


def _order(section: FeaturedGamesSection) -> list[str]:
    return [row.espn_id for row in section.rows]


def test_one_tier_1_player_beats_three_tier_2_players(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)
        for _ in range(3):
            world.prospect("t02", 2)
        tier_2_game = world.game("t02", "t10")
        tier_1_game = world.game("t01", "t11")

        section = world.featured()

    assert _order(section) == [tier_1_game, tier_2_game]


def test_between_two_tier_1_games_two_watchlist_players_beat_one(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)
        world.prospect("t02", 1)
        world.prospect("t03", 3)
        one = world.game("t01", "t10")
        # Players on both teams count.
        two = world.game("t02", "t03")

        section = world.featured()

    assert _order(section) == [two, one]
    assert [len(row.players) for row in section.rows] == [2, 1]


def test_with_tier_and_count_equal_the_weaker_team_decides(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)
        world.prospect("t02", 1)
        world.ranks({"t01": 5, "t10": 120, "t02": 10, "t11": 40})
        five_vs_120 = world.game("t01", "t10")
        ten_vs_40 = world.game("t02", "t11")

        section = world.featured()

    assert _order(section) == [ten_vs_40, five_vs_120]
    assert section.rows[0].home == FeaturedTeam(slug="t02", display_name="Team T02", rank=10)
    assert section.rows[0].away == FeaturedTeam(slug="t11", display_name="Team T11", rank=40)


def test_an_unmapped_or_unranked_team_counts_as_999(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        for team in ("t01", "t02", "t03"):
            world.prospect(team, 1)
        world.ranks({"t01": 1, "t02": 2, "t03": 3, "t10": 300})
        non_d1 = world.game("t01", None, away_name="Small College")
        unranked = world.game("t02", "t11", start=TIP - timedelta(hours=1))
        ranked_300 = world.game("t03", "t10")

        section = world.featured()

    # Both 999s tie on rank, so tip time decides between them.
    assert _order(section) == [ranked_300, unranked, non_d1]
    # A team the crosswalk does not know shows under ESPN's name; a known one under CBBD's.
    assert section.rows[2].away == FeaturedTeam(slug=None, display_name="Small College", rank=None)
    assert section.rows[2].home == FeaturedTeam(slug="t01", display_name="Team T01", rank=1)
    assert section.rows[1].away == FeaturedTeam(slug="t11", display_name="Team T11", rank=None)


def test_with_everything_else_equal_the_earlier_tip_comes_first(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 2)
        world.prospect("t02", 2)
        later = world.game("t01", "t10", start=TIP + timedelta(days=1))
        earlier = world.game("t02", "t11")

        section = world.featured()

    assert _order(section) == [earlier, later]


def test_seven_candidates_give_five_and_two_more(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        ids: list[str] = []
        for n in range(7):
            world.prospect(SLUGS[n], 1)
            ids.append(world.game(SLUGS[n], SLUGS[n + 10], start=TIP + timedelta(hours=n)))

        section = world.featured()

    assert _order(section) == ids[:5]
    assert section.more_count == 2


def test_a_row_carries_start_et_networks_venue_and_players(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 2, name="Zed Last")
        world.prospect("t01", 1, name="Abe First")
        world.prospect("t10", 2, name="Ann Other")
        world.game("t01", "t10", networks=("ESPN", "ESPN+"), venue="Allen Fieldhouse")
        bare = world.game("t02", "t03", networks=(), venue=None)
        world.prospect("t02", 3)

        section = world.featured()

    row = section.rows[0]
    assert row.start == datetime(2026, 11, 10, 19, 0, tzinfo=EASTERN)
    assert row.start.utcoffset() == timedelta(hours=-5)
    assert row.networks == "ESPN/ESPN+"
    assert row.venue == "Allen Fieldhouse"
    assert [(p.name, p.tier, p.team_slug) for p in row.players] == [
        ("Abe First", 1, "t01"),
        ("Ann Other", 2, "t10"),
        ("Zed Last", 2, "t01"),
    ]
    no_broadcast = section.rows[1]
    assert no_broadcast.espn_id == bare
    assert no_broadcast.networks == "—"
    assert no_broadcast.venue is None


def test_only_this_weeks_games_with_a_resolved_player_qualify(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)
        world.unresolvable("t05", 1)
        world.game("t01", "t10", start=datetime(2026, 11, 9, 4, 59, tzinfo=UTC))  # Sun ET
        world.game("t01", "t11", start=datetime(2026, 11, 16, 5, 0, tzinfo=UTC))  # next Mon
        world.game("t05", "t12")  # only an unresolved player
        world.game("t20", "t21")  # nobody on the watchlist
        first = world.game("t01", "t13", start=datetime(2026, 11, 9, 5, 0, tzinfo=UTC))
        last = world.game("t01", "t14", start=datetime(2026, 11, 16, 4, 59, 59, tzinfo=UTC))

        section = world.featured()

    assert _order(section) == [first, last]
    assert section.more_count == 0


def test_a_player_who_changed_teams_counts_for_the_current_team(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1, now_on="t02")
        world.game("t01", "t10")
        current = world.game("t02", "t11")

        section = world.featured()

    assert _order(section) == [current]
    assert section.rows[0].players[0].team_slug == "t02"


def test_without_ranks_every_team_is_unranked(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)
        world.prospect("t02", 1)
        world.ranks({"t01": 1, "t10": 2, "t02": 3, "t11": 300})
        ranked_300 = world.game("t02", "t11")
        later = world.game("t01", "t10", start=TIP + timedelta(hours=1))

        section = world.featured(ranked=False)

    assert _order(section) == [ranked_300, later]
    assert all(row.home.rank is None and row.away.rank is None for row in section.rows)


def test_no_candidates_give_an_empty_section(engine: Engine) -> None:
    with engine.begin() as connection:
        world = _World(connection)
        world.prospect("t01", 1)

        section = world.featured()

    assert section == FeaturedGamesSection()


def test_a_run_without_windows_is_refused(engine: Engine) -> None:
    with engine.begin() as connection:
        with pytest.raises(LookupError):
            featured_games(connection, "2027-W99", Watchlist(season=SEASON, players=()))
        with pytest.raises(ValueError):
            featured_games(connection, RUN, Watchlist(season=SEASON + 1, players=()))
