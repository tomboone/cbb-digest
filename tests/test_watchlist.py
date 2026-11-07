"""The watchlist file, its validation, and name normalisation (spec §7.1, §7.2)."""

from pathlib import Path

import pytest

from cbb_digest.watchlist import (
    WATCHLIST_PATH,
    Watchlist,
    WatchlistError,
    WatchlistPlayer,
    load_watchlist,
    normalise_name,
    watchlist_key,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _player(name: str = "Tyran Stokes", team: str | None = "Kansas") -> WatchlistPlayer:
    return WatchlistPlayer.model_validate(
        {"name": name, "team": team, "tier": 1, "class": 2027}, context={"season": 2027}
    )


# Normalisation (§7.2).


def test_a_suffix_and_case_do_not_change_the_normalised_name() -> None:
    assert normalise_name("Jordan Smith Jr.") == normalise_name("jordan smith")


def test_accents_apostrophes_and_a_roman_numeral_are_dropped() -> None:
    assert normalise_name("Dāvis O'Neal III") == "davis oneal"


@pytest.mark.parametrize(
    ("name", "normalised"),
    [
        ("A.J. Storr", "aj storr"),
        ("Ja\u2019Kobe  Walter", "jakobe walter"),
        ("  Cooper\tFlagg  ", "cooper flagg"),
        ("Ace Bailey Sr", "ace bailey"),
        ("Henri Veesaar II", "henri veesaar"),
        ("Jordan Smith IV", "jordan smith"),
        ("Straße", "strasse"),
    ],
)
def test_normalise_name(name: str, normalised: str) -> None:
    assert normalise_name(name) == normalised


def test_a_suffix_inside_a_name_is_kept() -> None:
    assert normalise_name("Iverson Jr Smith") == "iverson jr smith"


def test_a_lone_suffix_is_not_dropped_to_nothing() -> None:
    assert normalise_name("Jr.") == "jr"


# The watchlist key (§7.3).


def test_the_key_is_the_normalised_name_and_the_team_slug() -> None:
    assert watchlist_key(_player("Tyran Stokes Jr."), "kansas") == "tyran stokes|kansas"


def test_the_key_of_a_blank_team_is_a_dash() -> None:
    assert watchlist_key(_player(team=None), None) == "tyran stokes|—"


def test_a_stated_team_without_a_slug_has_no_key() -> None:
    # An unmapped team keyed as `—` would collide with a blank-team entry of the same name.
    with pytest.raises(ValueError, match="does not match the stated team"):
        watchlist_key(_player(team="Nowhere St."), None)


def test_a_slug_for_a_blank_team_is_refused() -> None:
    with pytest.raises(ValueError, match="does not match the stated team"):
        watchlist_key(_player(team=None), "kansas")


# Loading the file (§7.1).


def test_the_committed_watchlist_loads() -> None:
    assert WATCHLIST_PATH.name == "watchlist.yaml"
    assert isinstance(load_watchlist(), Watchlist)


def test_a_valid_file_loads(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
season: 2027
players:
  - name: Tyran Stokes
    team: Kansas
    tier: 1
    class: 2027
    note: "optional free text"
  - name: Darryn Peterson
    tier: 2
    class: 2028
""",
    )

    watchlist = load_watchlist(path)

    assert watchlist.season == 2027
    assert [player.model_dump() for player in watchlist.players] == [
        {
            "name": "Tyran Stokes",
            "team": "Kansas",
            "tier": 1,
            "class_": 2027,
            "note": "optional free text",
        },
        {"name": "Darryn Peterson", "team": None, "tier": 2, "class_": 2028, "note": None},
    ]


def test_a_blank_team_is_no_team(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
season: 2027
players:
  - {name: A, team: "", tier: 1, class: 2027}
  - {name: B, team: "  ", tier: 1, class: 2027}
  - {name: C, team: , tier: 1, class: 2027}
""",
    )

    assert [player.team for player in load_watchlist(path).players] == [None, None, None]


def test_an_empty_player_list_loads(tmp_path: Path) -> None:
    assert load_watchlist(_write(tmp_path, "season: 2027\nplayers: []\n")).players == ()


def test_every_error_is_reported_at_once(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
season: 2027
players:
  - {name: Tyran Stokes, team: Kansas, tier: 4, class: 2027}
  - {name: Darryn Peterson, team: Kansas, tier: 1, class: 2026}
  - {name: Jordan Smith, team: Duke, tier: 2, class: 2027}
  - {name: Jordan Smith Jr., team: Duke, tier: 3, class: 2027}
""",
    )

    with pytest.raises(WatchlistError) as caught:
        load_watchlist(path)

    errors = caught.value.errors
    assert len(errors) == 3
    assert errors[0].startswith("players[0] (Tyran Stokes): tier:")
    assert errors[1].startswith("players[1] (Darryn Peterson): class: 2026 is before season 2027")
    assert errors[2] == (
        "players[3] (Jordan Smith Jr.): same normalised name and team as players[2]"
    )
    assert str(path) in str(caught.value)


def test_one_entry_reports_each_of_its_errors(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "season: 2027\nplayers:\n  - {name: Tyran Stokes, tier: 4, class: 2026}\n",
    )

    with pytest.raises(WatchlistError) as caught:
        load_watchlist(path)

    assert [error.split(": ")[1] for error in caught.value.errors] == ["tier", "class"]


def test_a_duplicate_is_matched_on_the_team_regardless_of_case(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
season: 2027
players:
  - {name: Jordan Smith, team: Duke, tier: 2, class: 2027}
  - {name: Jordan Smith, team: " duke", tier: 2, class: 2027}
  - {name: Jordan Smith, team: Kansas, tier: 2, class: 2027}
  - {name: Jordan Smith, tier: 2, class: 2027}
""",
    )

    with pytest.raises(WatchlistError) as caught:
        load_watchlist(path)

    assert caught.value.errors == [
        "players[1] (Jordan Smith): same normalised name and team as players[0]"
    ]


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ("{team: Kansas, tier: 1, class: 2027}", "name"),
        ("{name: '', tier: 1, class: 2027}", "name"),
        ("{name: '   ', tier: 1, class: 2027}", "name"),
        ("{name: A, class: 2027}", "tier"),
        ("{name: A, tier: '1', class: 2027}", "tier"),
        ("{name: A, tier: 0, class: 2027}", "tier"),
        ("{name: A, tier: 1}", "class"),
        ("{name: A, tier: 1, class: '2027'}", "class"),
        ("{name: A, tier: 1, class: 2027, teir: 2}", "teir"),
        ("{name: A, tier: 1, class: 2027, note: 12}", "note"),
    ],
)
def test_an_invalid_entry_is_refused(tmp_path: Path, entry: str, field: str) -> None:
    path = _write(tmp_path, f"season: 2027\nplayers:\n  - {entry}\n")

    with pytest.raises(WatchlistError) as caught:
        load_watchlist(path)

    [error] = caught.value.errors
    assert error.startswith("players[0]")
    assert f": {field}:" in error


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("players: []\n", "season: Field required"),
        ("season: '2027'\nplayers: []\n", "season: Input should be a valid integer"),
        ("season: true\nplayers: []\n", "season: Input should be a valid integer"),
        ("season: 2027\n", "players: Field required"),
        ("season: 2027\nplayers: {name: A}\n", "players: Input should be a valid list"),
        ("season: 2027\nplayers: []\nseasn: 2028\n", "seasn: Extra inputs are not permitted"),
        ("season: 2027\nplayers:\n  - Tyran Stokes\n", "players[0]: Input should be"),
        ("- season: 2027\n", "the file is not a mapping"),
        ("", "the file is not a mapping"),
        ("season: [2027\n", "not valid YAML"),
    ],
)
def test_an_invalid_file_is_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(WatchlistError) as caught:
        load_watchlist(_write(tmp_path, text))

    assert any(message in error for error in caught.value.errors), caught.value.errors


def test_an_invalid_season_still_reports_the_entries(tmp_path: Path) -> None:
    path = _write(tmp_path, "season: soon\nplayers:\n  - {name: A, tier: 9, class: 2027}\n")

    with pytest.raises(WatchlistError) as caught:
        load_watchlist(path)

    assert [error.split(":")[0] for error in caught.value.errors] == ["season", "players[0] (A)"]


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WatchlistError, match="cannot read"):
        load_watchlist(tmp_path / "missing.yaml")
