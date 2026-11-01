"""`fetch` then `load` over the saved fixtures, and the loader's rules (spec §9.1, §9.4).

The fixtures mix seasons: Torvik's files and CBBD's ratings are season 2026, CBBD's teams are
season 2027, and the ESPN day is 2026-11-03. The end-to-end test runs season 2026 for the week of
2026-11-02, serving the 2027 teams for it, so `Saint Francis` (Torvik 2026 only) is the one
unmapped Torvik team.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, insert, select
from typer.testing import CliRunner

from cbb_digest import cli, db, watchlist
from cbb_digest.cli import app
from cbb_digest.load import (
    OTHER_SEASON,
    UNMAPPED_OPPONENT,
    UNMAPPED_SIDE,
    UNMAPPED_TEAM,
    LoadError,
    LoadInput,
    LoadReport,
    load,
)
from cbb_digest.schema import (
    games,
    player_games,
    players,
    runs,
    team_ratings_snapshot,
    teams,
)
from cbb_digest.sources import cbbd, espn, http, torvik
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.sources.models.cbbd_rating import CbbdRating
from cbb_digest.sources.models.espn_scoreboard import EspnGame, EspnTeam
from cbb_digest.sources.models.torvik_player_game import TorvikPlayerGame
from cbb_digest.sources.models.torvik_player_season import TorvikPlayerSeason
from cbb_digest.teams import CanonicalTeam, TeamOverride, load_overrides
from cbb_digest.weeks import windows_for

FIXTURES = Path(__file__).parent / "fixtures"
MONDAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 11, 2, 11, 0, tzinfo=UTC)

runner = CliRunner()


# --- fetch, then load twice, over the fixtures ---


def _json(path: str) -> httpx.Response:
    body = (FIXTURES / path).read_bytes()
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


def _scoreboard(request: httpx.Request) -> httpx.Response:
    if request.url.params["dates"] == "20261103":
        return _json("espn/scoreboard-2026-11-01.json")
    return httpx.Response(200, json={"events": []})


@pytest.fixture
def sources() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{torvik.BASE_URL}{torvik.PLAYER_SEASON_PATH}").respond(
            200,
            content=(FIXTURES / "torvik/getadvstats-y2026-2026-11-01.csv").read_bytes(),
            headers={"Content-Type": "text/csv"},
        )
        router.get(f"{torvik.BASE_URL}{torvik.player_games_path(2026)}").mock(
            return_value=_json("torvik/all_advgames-y2026-2026-11-01.json")
        )
        router.get(f"{cbbd.BASE_URL}{cbbd.TEAMS_PATH}").mock(
            return_value=_json("cbbd/teams-y2027-2026-11-01.json")
        )
        router.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}").mock(
            return_value=_json("cbbd/ratings-adjusted-y2026-2026-11-01.json")
        )
        router.get(f"{espn.BASE_URL}{espn.SCOREBOARD_PATH}").mock(side_effect=_scoreboard)
        yield router


def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CBBD_API_KEY", "test-key")
    monkeypatch.setenv("RECIPIENTS", "tom@example.com")
    path = tmp_path / "watchlist.yaml"
    path.write_text("season: 2026\nplayers: []\n", encoding="utf-8")
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)
    monkeypatch.setattr(http.time, "sleep", _no_sleep)
    return data_dir


def _lines(output: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in output.splitlines() if ": " in line)


def _count(engine: Engine, table: Any) -> int:
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


@pytest.mark.usefixtures("sources")
def test_fetch_then_load_fills_every_table_and_a_second_load_changes_nothing(env: Path) -> None:
    fetched = runner.invoke(app, ["fetch", "--week", "2026-11-03"])

    assert fetched.exit_code == 0, fetched.output
    lines = _lines(fetched.output)
    assert lines["torvik player season"] == "OK (440 rows)"
    assert lines["cbbd teams"] == "OK (365 rows)"
    assert lines["espn week"] == "OK (29 rows)"
    # 2 Torvik, 1 ratings, 1 teams (the table is empty), 7 ESPN days.
    assert lines["requests"] == "11"

    first = runner.invoke(app, ["load", "--week", "2026-11-03"])

    assert first.exit_code == 0, first.output
    assert "week of 2026-11-02, run 2026-W45" in first.output
    lines = _lines(first.output)
    assert lines["teams"] == "365 changed"
    assert lines["players"].startswith("439 changed (1 unmapped team)")
    for table in ("player_games", "games", "team_ratings_snapshot"):
        assert not lines[table].startswith("0 changed"), lines[table]
    engine = db.open_database(env)
    try:
        for table in (teams, players, player_games, games, team_ratings_snapshot, runs):
            assert _count(engine, table) > 0, table.name
        with engine.connect() as connection:
            named = connection.execute(
                select(teams.c.espn_name).where(teams.c.slug == "tennessee")
            ).scalar_one()
        assert named == "Tennessee"
    finally:
        engine.dispose()

    second = runner.invoke(app, ["load", "--week", "2026-11-03"])

    assert second.exit_code == 0, second.output
    lines = _lines(second.output)
    for table in ("teams", "players", "player_games", "games", "team_ratings_snapshot"):
        assert lines[table].startswith("0 changed"), (table, lines[table])


@pytest.mark.usefixtures("sources")
def test_a_second_fetch_inside_the_window_makes_no_requests_and_skips_teams(env: Path) -> None:
    runner.invoke(app, ["fetch", "--week", "2026-11-03"])
    runner.invoke(app, ["load", "--week", "2026-11-03"])

    again = runner.invoke(app, ["fetch", "--week", "2026-11-03"])

    assert again.exit_code == 0, again.output
    lines = _lines(again.output)
    assert lines["requests"] == "0"
    assert "cbbd teams" not in lines


def test_fetch_reports_a_failed_source_and_exits_1(env: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{torvik.BASE_URL}{torvik.PLAYER_SEASON_PATH}").respond(
            200, text="<title>Verifying Browser...</title>", headers={"Content-Type": "text/html"}
        )
        router.route().respond(503)

        result = runner.invoke(app, ["fetch", "--week", "2026-11-03"])

    assert result.exit_code == 1
    assert _lines(result.output)["torvik player season"].startswith("FAIL (challenge")


def test_load_without_a_fetch_skips_every_source_and_exits_1(env: Path) -> None:
    result = runner.invoke(app, ["load", "--week", "2026-11-03"])

    assert result.exit_code == 1
    assert "`teams` is empty" in result.output


@pytest.mark.usefixtures("sources")
def test_load_skips_a_source_missing_from_the_cache_and_loads_the_rest(env: Path) -> None:
    runner.invoke(app, ["fetch", "--week", "2026-11-03"])
    (env / "cache" / "espn" / "scoreboard-20261104").unlink()

    result = runner.invoke(app, ["load", "--week", "2026-11-03"])

    assert result.exit_code == 1
    lines = _lines(result.output)
    assert lines["espn week"].startswith("skipped (ConnectError: not in the cache")
    assert "games" not in lines
    assert lines["players"].startswith("439 changed")


@pytest.mark.usefixtures("sources")
def test_a_crosswalk_edit_applies_on_the_next_load_without_refetching_teams(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["fetch", "--week", "2026-11-03"])
    runner.invoke(app, ["load", "--week", "2026-11-03"])
    (env / "cache" / "cbbd" / "teams-2026").unlink()
    edited = {**load_overrides(), "tennessee": TeamOverride(torvik_name="Tenn.")}
    monkeypatch.setattr(cli, "load_overrides", lambda: edited)

    result = runner.invoke(app, ["load", "--week", "2026-11-03"])

    assert result.exit_code == 0, result.output
    assert _lines(result.output)["teams"] == "1 changed"
    engine = db.open_database(env)
    try:
        with engine.connect() as connection:
            torvik_name = connection.execute(
                select(teams.c.torvik_name).where(teams.c.slug == "tennessee")
            ).scalar_one()
    finally:
        engine.dispose()
    assert torvik_name == "Tenn."


# --- the loader's rules ---


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    yield engine
    engine.dispose()


def _team(slug: str, cbbd_id: int, *, name: str | None = None, torvik: str | None = None) -> Any:
    school = name or slug.title()
    return CanonicalTeam(
        slug=slug,
        cbbd_id=cbbd_id,
        cbbd_name=school,
        torvik_name=torvik or school,
        espn_id=str(1000 + cbbd_id),
    )


TEAMS = [_team("iupui", 1, name="IUPUI"), _team("duke", 2), _team("iowa", 3)]


def _season_row(pid: int, team: str, *, season: int = 2027, games: int = 4) -> TorvikPlayerSeason:
    return TorvikPlayerSeason(
        pid=pid,
        season=season,
        name=f"Player {pid}",
        team=team,
        class_year="Fr",
        games=games,
        min_per_game=30.25,
        pts_per_game=15.5,
        reb_per_game=6.25,
        ast_per_game=2.0,
        fg2m=20,
        fg2a=40,
        fg3m=5,
        fg3a=15,
        fta=12,
    )


def _game_row(pid: int, team: str, opponent: str) -> TorvikPlayerGame:
    return TorvikPlayerGame(
        pid=pid,
        game_date=date(2026, 11, 3),
        team=team,
        opponent=opponent,
        min=31.5,
        pts=18,
        orb=1,
        drb=5,
        ast=3,
        fg2m=5,
        fg2a=9,
        fg3m=2,
        fg3a=6,
        fta=4,
    )


def _espn_game(espn_id: str, home: EspnTeam, away: EspnTeam) -> EspnGame:
    return EspnGame(
        espn_id=espn_id,
        start=datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
        home=home,
        away=away,
        venue="Cameron Indoor Stadium",
        networks=["ESPN", "ESPN+"],
        status="STATUS_SCHEDULED",
    )


def _rating(team_id: int, net: float) -> CbbdRating:
    return CbbdRating(team_id=team_id, net=net, adj_o=110.0 + net, adj_d=110.0)


NOT_FETCHED = SourceFailure("test", "not fetched")


def _input(
    *,
    canonical: list[CanonicalTeam] | None = TEAMS,
    player_season: Fetched[list[TorvikPlayerSeason]] = NOT_FETCHED,
    player_games_rows: Fetched[list[TorvikPlayerGame]] = NOT_FETCHED,
    ratings: Fetched[list[CbbdRating]] = NOT_FETCHED,
    espn_week: Fetched[list[EspnGame]] = NOT_FETCHED,
) -> LoadInput:
    return LoadInput(
        season=2027,
        run_id="2027-W45",
        windows=windows_for(MONDAY),
        canonical_teams=canonical,
        player_season=player_season,
        player_games=player_games_rows,
        ratings=ratings,
        ratings_fetched_at=FETCHED_AT,
        espn_week=espn_week,
    )


def _load(engine: Engine, data: LoadInput) -> LoadReport:
    with engine.begin() as connection:
        return load(connection, data, now=NOW)


def _teams(engine: Engine) -> dict[str, tuple[Any, ...]]:
    with engine.connect() as connection:
        return {row.slug: tuple(row)[1:] for row in connection.execute(select(teams))}


def test_load_needs_teams_from_somewhere(engine: Engine) -> None:
    with pytest.raises(LoadError, match="`teams` is empty"):
        _load(engine, _input(canonical=None))


def test_a_renamed_team_keeps_its_slug_and_its_references(engine: Engine) -> None:
    _load(engine, _input(player_season=[_season_row(7, "IUPUI")]))
    renamed = _team("iu-indianapolis", 1, name="IU Indianapolis", torvik="IU Indy")

    report = _load(engine, _input(canonical=[renamed, *TEAMS[1:]]))

    assert report.tables["teams"].changed == 1
    stored = _teams(engine)
    assert "iu-indianapolis" not in stored
    assert stored["iupui"] == (1, "IU Indianapolis", "IU Indy", "1001", None)
    with engine.connect() as connection:
        assert connection.execute(select(players.c.team_slug)).scalar_one() == "iupui"


def test_two_teams_can_swap_torvik_names(engine: Engine) -> None:
    _load(engine, _input())
    swapped = [TEAMS[0], _team("duke", 2, torvik="Iowa"), _team("iowa", 3, torvik="Duke")]

    report = _load(engine, _input(canonical=swapped))

    assert report.tables["teams"].changed == 2
    stored = _teams(engine)
    assert stored["duke"][2] == "Iowa"
    assert stored["iowa"][2] == "Duke"


def test_two_teams_can_swap_espn_ids(engine: Engine) -> None:
    _load(engine, _input())
    duke, iowa = TEAMS[1], TEAMS[2]
    swapped = [
        TEAMS[0],
        CanonicalTeam("duke", 2, "Duke", "Duke", iowa.espn_id),
        CanonicalTeam("iowa", 3, "Iowa", "Iowa", duke.espn_id),
    ]

    report = _load(engine, _input(canonical=swapped))

    assert report.tables["teams"].changed == 2
    stored = _teams(engine)
    assert (stored["duke"][3], stored["iowa"][3]) == (iowa.espn_id, duke.espn_id)


def test_a_new_team_whose_slug_another_team_holds_is_refused(engine: Engine) -> None:
    _load(engine, _input())

    with pytest.raises(LoadError, match=r"'duke' .* already held by CBBD team 2"):
        _load(engine, _input(canonical=[*TEAMS, _team("duke", 9, torvik="Duke 2")]))


def test_espn_name_is_filled_from_the_week_and_a_non_d1_side_is_ignored(engine: Engine) -> None:
    week = [
        _espn_game(
            "1",
            EspnTeam(espn_id="1002", name="Duke", conference_id="2"),
            EspnTeam(espn_id="99999", name="Small College", conference_id=None),
        ),
        _espn_game(
            "2",
            EspnTeam(espn_id="1003", name="Iowa", conference_id="7"),
            EspnTeam(espn_id="55555", name="Unknown D-I", conference_id="9"),
        ),
    ]

    report = _load(engine, _input(espn_week=week))

    assert _teams(engine)["duke"][4] == "Duke"
    assert report.tables["games"].changed == 2
    assert report.tables["games"].notes == {UNMAPPED_SIDE: 1}
    with engine.connect() as connection:
        rows = {row.espn_id: row for row in connection.execute(select(games))}
    assert (rows["1"].home_slug, rows["1"].away_slug) == ("duke", None)
    assert rows["1"].networks == ["ESPN", "ESPN+"]

    again = _load(engine, _input(espn_week=week))

    assert again.tables["teams"].changed == 0
    assert again.tables["games"].changed == 0


def test_players_from_another_season_or_an_unmapped_team_are_dropped(engine: Engine) -> None:
    rows = [
        _season_row(1, "Duke"),
        _season_row(2, "Duke", season=2026),
        _season_row(3, "Saint Francis"),
    ]

    report = _load(engine, _input(player_season=rows))

    assert report.tables["players"].changed == 1
    assert report.tables["players"].notes == {OTHER_SEASON: 1, UNMAPPED_TEAM: 1}
    with engine.connect() as connection:
        player = connection.execute(select(players)).one()
    assert player.torvik_pid == 1
    assert player.normalized_name == "player 1"
    # Per-game averages times games played.
    assert (player.min, player.pts, player.reb, player.ast) == (121.0, 62, 25, 8)
    assert (player.fga, player.fta) == (55, 12)


def test_a_changed_player_is_updated_and_only_it_is_counted(engine: Engine) -> None:
    _load(engine, _input(player_season=[_season_row(1, "Duke"), _season_row(2, "Iowa")]))

    report = _load(
        engine, _input(player_season=[_season_row(1, "Duke", games=5), _season_row(2, "Iowa")])
    )

    assert report.tables["players"].changed == 1


def test_game_logs_drop_unmapped_opponents_and_players_without_a_row(engine: Engine) -> None:
    rows = [
        _game_row(1, "Duke", "Iowa"),
        _game_row(1, "Duke", "Nowhere State"),
        _game_row(2, "Iowa", "Duke"),
    ]

    report = _load(engine, _input(player_season=[_season_row(1, "Duke")], player_games_rows=rows))

    assert report.tables["player_games"].changed == 1
    assert report.tables["player_games"].notes == {UNMAPPED_OPPONENT: 1, "no player row": 1}
    with engine.connect() as connection:
        game = connection.execute(select(player_games)).one()
    assert (game.opponent_slug, game.reb, game.fgm, game.fga) == ("iowa", 6, 7, 15)


def test_the_snapshot_is_ranked_and_replaced_for_its_run(engine: Engine) -> None:
    first = _load(engine, _input(ratings=[_rating(2, 20.5), _rating(3, 25.0)]))

    assert first.tables["team_ratings_snapshot"].changed == 2
    with engine.connect() as connection:
        run = connection.execute(select(runs)).one()
        snapshot = connection.execute(select(team_ratings_snapshot)).all()
    ranks = {row.slug: row.rank for row in snapshot}
    assert (run.run_id, run.status, run.kind) == ("2027-W45", "running", "digest")
    assert run.this_week_start == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    assert ranks == {"iowa": 1, "duke": 2}

    again = _load(engine, _input(ratings=[_rating(3, 25.0), _rating(2, 20.5)]))
    replaced = _load(engine, _input(ratings=[_rating(1, 30.0)]))

    assert again.tables["team_ratings_snapshot"].changed == 0
    assert replaced.tables["team_ratings_snapshot"].changed == 3
    with engine.connect() as connection:
        rows = connection.execute(select(team_ratings_snapshot.c.slug)).scalars().all()
    assert rows == ["iupui"]


def test_an_existing_run_row_is_left_alone(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id="2027-W45", kind="digest", season=2027, status="sent", started_at=NOW
            )
        )

    _load(engine, _input(ratings=[_rating(2, 20.5)]))

    with engine.connect() as connection:
        assert connection.execute(select(runs.c.status)).scalar_one() == "sent"
