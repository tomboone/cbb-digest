"""Resolution of the watchlist against `players` (spec §7.3) and its alerts (§7.4)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, update
from typer.testing import CliRunner

from cbb_digest import db, watchlist
from cbb_digest.cli import app
from cbb_digest.resolve import (
    ResolvedPlayer,
    ResolveError,
    ResolveReport,
    resolve,
    resolved_players,
)
from cbb_digest.schema import player_games, players, runs, teams, watchlist_resolution
from cbb_digest.watchlist import Watchlist, WatchlistPlayer, normalise_name
from cbb_digest.weeks import windows_for

SEASON = 2027
RUN = "2027-W45"
NEXT_RUN = "2027-W46"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
MONDAY = date(2026, 11, 2)

runner = CliRunner()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            insert(teams),
            [
                _team("kansas", 1, "Kansas"),
                _team("duke", 2, "Duke"),
                _team("iowa-state", 3, "Iowa State", torvik="Iowa St."),
                _team("tennessee", 4, "Tennessee"),
            ],
        )
    yield engine
    engine.dispose()


def _team(slug: str, cbbd_id: int, name: str, *, torvik: str | None = None) -> dict[str, Any]:
    return {
        "slug": slug,
        "cbbd_id": cbbd_id,
        "cbbd_name": name,
        "torvik_name": torvik or name,
        "espn_id": str(1000 + cbbd_id),
    }


def _add_player(
    engine: Engine, pid: int, name: str, team_slug: str, *, played: bool = True
) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(players).values(
                season=SEASON,
                torvik_pid=pid,
                name=name,
                normalized_name=normalise_name(name),
                team_slug=team_slug,
                class_year="Fr",
                games=1,
                min=30.0,
                pts=10,
                reb=5,
                ast=3,
                fga=8,
                fta=2,
            )
        )
        if played:
            connection.execute(
                insert(player_games).values(
                    season=SEASON,
                    torvik_pid=pid,
                    game_date=date(2026, 11, 3),
                    opponent_slug="tennessee",
                    min=30.0,
                    pts=10,
                    reb=5,
                    ast=3,
                    fgm=4,
                    fga=8,
                    fg3m=1,
                    fg3a=3,
                    fta=2,
                )
            )


def _watchlist(*entries: tuple[str, str | None]) -> Watchlist:
    return Watchlist(
        season=SEASON,
        players=tuple(
            WatchlistPlayer.model_validate({"name": name, "team": team, "tier": 1, "class": 2027})
            for name, team in entries
        ),
    )


def _resolve(engine: Engine, wl: Watchlist, run_id: str = RUN) -> ResolveReport:
    with engine.begin() as connection:
        return resolve(connection, wl, run_id=run_id, windows=windows_for(MONDAY), now=NOW)


def _alerts(report: ResolveReport) -> list[str]:
    return [str(alert) for alert in report.alerts]


def _row(engine: Engine, key: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(watchlist_resolution).where(watchlist_resolution.c.watchlist_key == key)
            )
            .mappings()
            .one()
        )
    return dict(row)


# --- the four steps ---


def test_step_1_an_exact_name_on_the_stated_team_resolves_without_an_alert(
    engine: Engine,
) -> None:
    _add_player(engine, 11, "Tyran Stokes", "kansas")
    _add_player(engine, 12, "Tyran Stokes", "duke")

    report = _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))

    assert _alerts(report) == []
    row = _row(engine, "tyran stokes|kansas")
    assert row["outcome"] == "resolved"
    assert (row["torvik_pid"], row["team_slug"], row["matched_name"]) == (
        11,
        "kansas",
        "Tyran Stokes",
    )
    assert row["alert_kind"] is None
    assert row["first_run_id"] == row["changed_run_id"] == RUN


def test_step_2_the_exact_name_on_another_team_resolves_with_team_changed(
    engine: Engine,
) -> None:
    _add_player(engine, 21, "Darryn Peterson", "duke")

    report = _resolve(engine, _watchlist(("Darryn Peterson", "Kansas")))

    assert _alerts(report) == ["team_changed: Darryn Peterson (was Kansas, now Duke)"]
    row = _row(engine, "darryn peterson|kansas")
    assert (row["outcome"], row["torvik_pid"], row["team_slug"]) == ("resolved", 21, "duke")
    assert row["alert_kind"] == "team_changed"


def test_step_2_with_no_stated_team_resolves_without_an_alert(engine: Engine) -> None:
    _add_player(engine, 22, "Cameron Boozer", "duke")

    report = _resolve(engine, _watchlist(("Cameron Boozer", None)))

    assert _alerts(report) == []
    assert _row(engine, "cameron boozer|—")["torvik_pid"] == 22


def test_step_3_a_close_spelling_on_the_stated_team_resolves_with_fuzzy_match(
    engine: Engine,
) -> None:
    _add_player(engine, 31, "John Smith", "iowa-state")
    _add_player(engine, 32, "Jon Smyth-Watkins", "iowa-state")

    report = _resolve(engine, _watchlist(("Jon Smith", "Iowa St.")))

    assert _alerts(report) == ['fuzzy_match: Jon Smith (matched "John Smith")']
    row = _row(engine, "jon smith|iowa-state")
    assert (row["outcome"], row["torvik_pid"], row["matched_name"]) == (
        "resolved",
        31,
        "John Smith",
    )


def test_step_3_never_matches_a_close_spelling_on_another_team(engine: Engine) -> None:
    _add_player(engine, 33, "John Smith", "duke")

    report = _resolve(engine, _watchlist(("Jon Smith", "Kansas")))

    assert _row(engine, "jon smith|kansas")["outcome"] == "unresolved"
    assert [alert.kind for alert in report.alerts] == ["unresolved"]


def test_two_players_of_one_name_and_no_stated_team_are_ambiguous(engine: Engine) -> None:
    _add_player(engine, 41, "Jalen Johnson", "duke")
    _add_player(engine, 42, "Jalen Johnson Jr.", "tennessee")

    report = _resolve(engine, _watchlist(("Jalen Johnson", None)))

    assert _alerts(report) == [
        "ambiguous: Jalen Johnson (candidates: Jalen Johnson (Duke, 41), "
        "Jalen Johnson Jr. (Tennessee, 42))"
    ]
    row = _row(engine, "jalen johnson|—")
    assert (row["outcome"], row["torvik_pid"], row["team_slug"]) == ("ambiguous", None, None)


def test_two_close_spellings_on_the_stated_team_are_ambiguous(engine: Engine) -> None:
    _add_player(engine, 43, "John Smith", "kansas")
    _add_player(engine, 44, "Jon Smithe", "kansas")

    report = _resolve(engine, _watchlist(("Jon Smith", "Kansas")))

    assert [alert.kind for alert in report.alerts] == ["ambiguous"]


def test_a_player_whose_team_has_not_played_is_not_yet_in_torvik(engine: Engine) -> None:
    # Tennessee has a player row but no games; Kansas has neither.
    _add_player(engine, 51, "Someone Else", "tennessee", played=False)

    report = _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))

    assert _alerts(report) == [
        "unresolved: Tyran Stokes (not yet in Torvik's file (team has not played))"
    ]
    assert _row(engine, "tyran stokes|kansas")["outcome"] == "unresolved"


def test_a_player_missing_after_his_team_has_played_is_unresolved(engine: Engine) -> None:
    _add_player(engine, 52, "Someone Else", "kansas")

    report = _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))

    assert _alerts(report) == ["unresolved: Tyran Stokes (not in Torvik's file)"]


def test_a_team_counts_as_played_from_a_game_against_it(engine: Engine) -> None:
    _add_player(engine, 53, "Someone Else", "duke")  # played Tennessee

    report = _resolve(engine, _watchlist(("Nobody Known", "Tennessee")))

    assert _alerts(report) == ["unresolved: Nobody Known (not in Torvik's file)"]


def test_an_unmappable_stated_team_is_unresolved_with_unmapped_team_once(
    engine: Engine,
) -> None:
    # Never guessed, even though the name alone would match.
    _add_player(engine, 61, "Tyran Stokes", "kansas")
    wl = _watchlist(("Tyran Stokes", "Kanzas"))

    first = _resolve(engine, wl)
    second = _resolve(engine, wl, NEXT_RUN)

    assert _alerts(first) == ['unmapped_team: Tyran Stokes (no crosswalk entry for "Kanzas")']
    assert second.alerts == []
    row = _row(engine, "tyran stokes|?kanzas")
    assert (row["outcome"], row["torvik_pid"], row["alert_kind"]) == (
        "unresolved",
        None,
        "unmapped_team",
    )


def test_a_stated_team_naming_two_teams_is_unmapped(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(update(teams).where(teams.c.slug == "duke").values(espn_name="Kansas"))

    report = _resolve(engine, _watchlist(("Tyran Stokes", "kansas")))

    assert _alerts(report) == [
        'unmapped_team: Tyran Stokes ("kansas" names more than one team: Duke, Kansas)'
    ]


@pytest.mark.parametrize("stated", ["Iowa State", "Iowa St.", "iowa-state", "IOWA STATE"])
def test_the_stated_team_maps_by_slug_cbbd_or_torvik_name(engine: Engine, stated: str) -> None:
    _add_player(engine, 71, "Joshua Jefferson", "iowa-state")

    report = _resolve(engine, _watchlist(("Joshua Jefferson", stated)))

    assert _alerts(report) == []
    assert _row(engine, "joshua jefferson|iowa-state")["torvik_pid"] == 71


def test_resolve_refuses_an_empty_teams_table(tmp_path: Path) -> None:
    engine = db.open_database(tmp_path)
    try:
        with pytest.raises(ResolveError, match="load"):
            _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))
    finally:
        engine.dispose()


# --- alerts on change only (§7.4) ---


def test_a_second_resolve_over_unchanged_data_returns_no_alerts(engine: Engine) -> None:
    _add_player(engine, 81, "Darryn Peterson", "duke")
    _add_player(engine, 82, "John Smith", "kansas")
    wl = _watchlist(
        ("Darryn Peterson", "Kansas"), ("Jon Smith", "Kansas"), ("Tyran Stokes", "Iowa State")
    )

    first = _resolve(engine, wl)
    second = _resolve(engine, wl, NEXT_RUN)

    assert [alert.kind for alert in first.alerts] == ["team_changed", "fuzzy_match", "unresolved"]
    assert second.alerts == []
    row = _row(engine, "darryn peterson|kansas")
    assert row["first_run_id"] == row["changed_run_id"] == RUN
    # The table still shows every entry's standing alert.
    assert [entry.alert is not None and not entry.new for entry in second.entries] == [True] * 3


def test_a_forced_rerun_of_the_same_week_returns_that_weeks_alerts(engine: Engine) -> None:
    _add_player(engine, 91, "Darryn Peterson", "duke")
    wl = _watchlist(("Darryn Peterson", "Kansas"))

    first = _resolve(engine, wl)
    again = _resolve(engine, wl)

    assert _alerts(again) == _alerts(first) != []


def test_a_change_in_detail_raises_the_alert_again(engine: Engine) -> None:
    _add_player(engine, 101, "Darryn Peterson", "duke")
    wl = _watchlist(("Darryn Peterson", "Kansas"))
    _resolve(engine, wl)
    with engine.begin() as connection:
        connection.execute(
            update(players).where(players.c.torvik_pid == 101).values(team_slug="tennessee")
        )

    report = _resolve(engine, wl, NEXT_RUN)

    assert _alerts(report) == ["team_changed: Darryn Peterson (was Kansas, now Tennessee)"]
    row = _row(engine, "darryn peterson|kansas")
    # The outcome is still `resolved`: first produced in the first run, changed in this one.
    assert (row["first_run_id"], row["changed_run_id"]) == (RUN, NEXT_RUN)


def test_a_new_outcome_resets_the_first_run(engine: Engine) -> None:
    wl = _watchlist(("Tyran Stokes", "Kansas"))
    _resolve(engine, wl)  # unresolved: Kansas has not played
    _add_player(engine, 111, "Tyran Stokes", "kansas")

    report = _resolve(engine, wl, NEXT_RUN)

    assert report.alerts == []  # a plain exact match is not an alert
    row = _row(engine, "tyran stokes|kansas")
    assert (row["outcome"], row["first_run_id"], row["changed_run_id"]) == (
        "resolved",
        NEXT_RUN,
        NEXT_RUN,
    )


def test_a_respelled_match_updates_the_name_without_a_change(engine: Engine) -> None:
    _add_player(engine, 121, "Tyran Stokes", "kansas")
    wl = _watchlist(("Tyran Stokes", "Kansas"))
    _resolve(engine, wl)
    with engine.begin() as connection:
        connection.execute(
            update(players).where(players.c.torvik_pid == 121).values(name="Tyran Stokes Jr.")
        )

    _resolve(engine, wl, NEXT_RUN)

    row = _row(engine, "tyran stokes|kansas")
    assert (row["matched_name"], row["changed_run_id"]) == ("Tyran Stokes Jr.", RUN)


def test_resolve_inserts_a_placeholder_run_and_leaves_an_existing_one(engine: Engine) -> None:
    _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))
    with engine.begin() as connection:
        connection.execute(update(runs).values(status="sent"))

    _resolve(engine, _watchlist(("Tyran Stokes", "Kansas")))

    with engine.connect() as connection:
        assert connection.execute(select(runs.c.status)).scalars().all() == ["sent"]


# --- `cbb-digest resolve` ---


def test_resolved_players_reads_back_only_the_resolved_entries_in_watchlist_order(
    engine: Engine,
) -> None:
    _add_player(engine, 11, "Tyran Stokes", "kansas")
    _add_player(engine, 21, "Darryn Peterson", "duke")
    _add_player(engine, 31, "AJ Dybantsa", "iowa-state")
    wl = _watchlist(
        ("Darryn Peterson", "Kansas"),
        ("Nobody Here", "Duke"),
        ("Someone", "Kanzas"),
        ("A.J. Dybantsa", None),
        ("Tyran Stokes", "Kansas"),
    )
    _resolve(engine, wl)

    with engine.connect() as connection:
        found = resolved_players(connection, wl)

    by_name = {player.name: player for player in wl.players}
    assert found == [
        ResolvedPlayer(by_name["Darryn Peterson"], 21, "duke", "Darryn Peterson"),
        ResolvedPlayer(by_name["A.J. Dybantsa"], 31, "iowa-state", "AJ Dybantsa"),
        ResolvedPlayer(by_name["Tyran Stokes"], 11, "kansas", "Tyran Stokes"),
    ]


def test_the_resolve_command_prints_every_entry_and_its_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CBBD_API_KEY", "test-key")
    monkeypatch.setenv("RECIPIENTS", "tom@example.com")
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "season: 2027\n"
        "players:\n"
        "  - {name: Tyran Stokes, team: Kansas, tier: 1, class: 2027}\n"
        "  - {name: Darryn Peterson, team: Kansas, tier: 2, class: 2027}\n"
        "  - {name: Nobody Known, team: Kanzas, tier: 3, class: 2027}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)
    engine = db.open_database(data_dir)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(teams), [_team("kansas", 1, "Kansas"), _team("duke", 2, "Duke")]
            )
        _add_player(engine, 11, "Tyran Stokes", "kansas")
        _add_player(engine, 21, "Darryn Peterson", "duke")
    finally:
        engine.dispose()

    first = runner.invoke(app, ["resolve", "--week", "2026-11-03"])

    assert first.exit_code == 0, first.output
    assert "week of 2026-11-02, run 2027-W45" in first.output
    lines = first.output.splitlines()
    assert any("Tyran Stokes" in line and "resolved" in line and "Kansas" in line for line in lines)
    assert "new alerts: 2" in first.output
    assert "  team_changed: Darryn Peterson (was Kansas, now Duke)" in lines
    assert '  unmapped_team: Nobody Known (no crosswalk entry for "Kanzas")' in lines

    second = runner.invoke(app, ["resolve", "--week", "2026-11-10"])

    assert second.exit_code == 0, second.output
    assert "new alerts: 0" in second.output
    # Still in the table, without the `*` of a new alert.
    assert "  team_changed (was Kansas, now Duke)" in second.output
    assert (
        "  team_changed: Darryn Peterson (was Kansas, now Duke)" not in second.output.splitlines()
    )


def test_the_resolve_command_exits_1_before_any_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CBBD_API_KEY", "test-key")
    monkeypatch.setenv("RECIPIENTS", "tom@example.com")
    path = tmp_path / "watchlist.yaml"
    path.write_text("season: 2027\nplayers: []\n", encoding="utf-8")
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)

    result = runner.invoke(app, ["resolve"])

    assert result.exit_code == 1
    assert "load" in result.output
