"""The team crosswalk against the saved fixtures, and its rules (spec §7.5).

The Torvik season file is season 2026 and the CBBD teams are season 2027, so they differ by one
team each way: `Saint Francis` left D-I after 2026 and `West Florida` joined for 2027. The test
allows exactly those two, so a third mismatch is a missing override, not a season change.
"""

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cbb_digest.sources.models.cbbd_team import CbbdTeam, parse_teams
from cbb_digest.sources.models.torvik_player_season import parse_player_season
from cbb_digest.teams import (
    CROSSWALK_PATH,
    CanonicalTeam,
    Crosswalk,
    TeamOverride,
    build_teams,
    load_overrides,
    slugify,
)

FIXTURES = Path(__file__).parent / "fixtures"
CBBD_TEAMS = parse_teams((FIXTURES / "cbbd" / "teams-y2027-2026-11-01.json").read_bytes())
TORVIK_SEASON = parse_player_season(
    (FIXTURES / "torvik" / "getadvstats-y2026-2026-11-01.csv").read_bytes()
)
TORVIK_GAMES: list[list[Any]] = json.loads(
    (FIXTURES / "torvik" / "all_advgames-y2026-2026-11-01.json").read_bytes()
)
ESPN_SCOREBOARD: dict[str, Any] = json.loads(
    (FIXTURES / "espn" / "scoreboard-2026-11-01.json").read_bytes()
)

ONLY_IN_TORVIK_2026 = {"Saint Francis"}
ONLY_IN_CBBD_2027 = {"west-florida"}


@pytest.fixture(scope="module")
def teams() -> list[CanonicalTeam]:
    return build_teams(CBBD_TEAMS, load_overrides())


@pytest.fixture(scope="module")
def crosswalk(teams: list[CanonicalTeam]) -> Crosswalk:
    return Crosswalk.from_teams(teams)


def _espn_competitors() -> Iterator[dict[str, Any]]:
    for event in ESPN_SCOREBOARD["events"]:
        for competitor in event["competitions"][0]["competitors"]:
            yield competitor["team"]


def _cbbd(team_id: int, school: str, espn_id: str) -> CbbdTeam:
    return CbbdTeam(id=team_id, school=school, display_name=school, espn_id=espn_id)


# The crosswalk against the fixtures.


def test_every_cbbd_team_is_a_canonical_team(teams: list[CanonicalTeam]) -> None:
    assert len(teams) == len(CBBD_TEAMS) == 365


def test_every_d1_espn_team_maps_to_one_slug_and_no_other_team_maps(crosswalk: Crosswalk) -> None:
    competitors = list(_espn_competitors())
    # A competitor with no `conferenceId` is non-D-I (spec §6): not in the crosswalk.
    d1 = [team for team in competitors if "conferenceId" in team]
    non_d1 = [team for team in competitors if "conferenceId" not in team]

    assert d1
    assert non_d1
    assert [team["id"] for team in d1 if crosswalk.slug_for_espn(team["id"]) is None] == []
    assert [team["id"] for team in non_d1 if crosswalk.slug_for_espn(team["id"]) is not None] == []


def test_every_torvik_season_team_maps_to_one_slug(crosswalk: Crosswalk) -> None:
    names = {row.team for row in TORVIK_SEASON}

    assert len(names) == 365
    assert {name for name in names if crosswalk.slug_for_torvik(name) is None} == (
        ONLY_IN_TORVIK_2026
    )


def test_every_torvik_game_log_team_and_opponent_maps_to_one_slug(crosswalk: Crosswalk) -> None:
    # Index 47 is the player's team and 5 the opponent (tests/fixtures/README.md).
    names = {row[47] for row in TORVIK_GAMES} | {row[5] for row in TORVIK_GAMES}

    assert {name for name in names if crosswalk.slug_for_torvik(name) is None} <= (
        ONLY_IN_TORVIK_2026
    )


def test_every_canonical_torvik_name_is_a_torvik_team(teams: list[CanonicalTeam]) -> None:
    # The converse: an override with a mistyped Torvik name would leave a real Torvik team unmapped.
    torvik_names = {row.team for row in TORVIK_SEASON}

    assert {team.slug for team in teams if team.torvik_name not in torvik_names} == (
        ONLY_IN_CBBD_2027
    )


def test_every_cbbd_id_maps_to_its_slug(crosswalk: Crosswalk, teams: list[CanonicalTeam]) -> None:
    assert all(crosswalk.slug_for_cbbd(team.cbbd_id) == team.slug for team in teams)


def test_no_two_slugs_share_a_source_name_or_id(teams: list[CanonicalTeam]) -> None:
    for field in ("slug", "cbbd_id", "torvik_name", "espn_id"):
        counts = Counter(getattr(team, field) for team in teams)
        assert [value for value, count in counts.items() if count > 1] == [], field


def test_every_override_changes_the_torvik_name() -> None:
    # An override that repeats CBBD's `school` is dead weight that hides a real one.
    schools = {slugify(team.school): team.school for team in CBBD_TEAMS}

    redundant = [
        slug
        for slug, override in load_overrides().items()
        if override.torvik_name == schools.get(slug)
    ]

    assert redundant == []


def test_the_sample_teams_map_across_all_three_sources(crosswalk: Crosswalk) -> None:
    assert crosswalk.slug_for_torvik("Iowa St.") == "iowa-state"
    assert crosswalk.slug_for_torvik("Iowa State") is None
    assert crosswalk.slug_for_torvik("Connecticut") == "uconn"
    assert crosswalk.slug_for_torvik("Mississippi") == "ole-miss"
    assert crosswalk.slug_for_torvik("Duke") == "duke"
    # ESPN's `LSU New Orleans` is CBBD's `New Orleans`, joined by id rather than by name.
    assert crosswalk.slug_for_espn("2443") == "new-orleans"


# Building the crosswalk.


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("North Carolina", "north-carolina"),
        ("St. John's", "st-johns"),
        ("Hawai'i", "hawaii"),
        ("San José State", "san-jose-state"),
        ("Miami (OH)", "miami-oh"),
        ("Texas A&M-Corpus Christi", "texas-a-m-corpus-christi"),
    ],
)
def test_slugify_is_ascii_kebab_case(name: str, slug: str) -> None:
    assert slugify(name) == slug


def test_torvik_name_defaults_to_the_cbbd_school_and_espn_id_to_its_source_id() -> None:
    [team] = build_teams([_cbbd(1, "Iowa State", "66")], {})

    assert team == CanonicalTeam(
        slug="iowa-state", cbbd_id=1, cbbd_name="Iowa State", torvik_name="Iowa State", espn_id="66"
    )


def test_an_override_replaces_the_torvik_name() -> None:
    overrides = {"iowa-state": TeamOverride(torvik_name="Iowa St.")}

    [team] = build_teams([_cbbd(1, "Iowa State", "66")], overrides)

    assert team.torvik_name == "Iowa St."


def test_an_override_for_a_slug_cbbd_does_not_have_is_refused() -> None:
    overrides = {"iowa-st": TeamOverride(torvik_name="Iowa St.")}

    with pytest.raises(ValueError, match="iowa-st"):
        build_teams([_cbbd(1, "Iowa State", "66")], overrides)


def test_two_teams_with_one_torvik_name_are_refused() -> None:
    overrides = {"ole-miss": TeamOverride(torvik_name="Mississippi State")}
    cbbd = [_cbbd(1, "Ole Miss", "145"), _cbbd(2, "Mississippi State", "344")]

    with pytest.raises(ValueError, match="torvik_name"):
        build_teams(cbbd, overrides)


def test_two_teams_with_one_slug_are_refused() -> None:
    with pytest.raises(ValueError, match="slug"):
        build_teams([_cbbd(1, "St. John's", "2599"), _cbbd(2, "St Johns", "1")], {})


def test_two_teams_with_one_espn_id_are_refused() -> None:
    with pytest.raises(ValueError, match="espn_id"):
        build_teams([_cbbd(1, "Duke", "150"), _cbbd(2, "Kentucky", "150")], {})


def test_the_committed_crosswalk_is_the_default_path() -> None:
    assert CROSSWALK_PATH.is_file()
    assert CROSSWALK_PATH.name == "teams_crosswalk.yaml"


def test_an_unknown_override_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "teams_crosswalk.yaml"
    path.write_text("iowa-state: {torvik_nmae: Iowa St.}\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_overrides(path)


def test_an_empty_crosswalk_file_has_no_overrides(tmp_path: Path) -> None:
    path = tmp_path / "teams_crosswalk.yaml"
    path.write_text("# nothing yet\n", encoding="utf-8")

    assert load_overrides(path) == {}


def test_an_unknown_name_or_id_maps_to_none(crosswalk: Crosswalk) -> None:
    assert crosswalk.slug_for_torvik("Nowhere St.") is None
    assert crosswalk.slug_for_espn("0") is None
    assert crosswalk.slug_for_cbbd(-1) is None
