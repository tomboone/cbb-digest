import json
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from cbb_digest.sources import http
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.cbbd import (
    BASE_URL,
    RATINGS_PATH,
    ROSTERS_PATH,
    TEAMS_PATH,
    fetch_adjusted_ratings,
    fetch_rosters,
    fetch_teams,
    make_cbbd_client,
)
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.sources.models.cbbd_rating import CbbdRating, RankedRating, rank_ratings
from cbb_digest.sources.models.cbbd_roster import CbbdRosterPlayer
from cbb_digest.sources.models.cbbd_team import CbbdTeam
from tests.fakes import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "cbbd"
TEAMS_2027 = (FIXTURES / "teams-y2027-2026-11-01.json").read_bytes()
RATINGS_2026 = (FIXTURES / "ratings-adjusted-y2026-2026-11-01.json").read_bytes()
RATINGS_2027 = (FIXTURES / "ratings-adjusted-y2027-2026-11-01.json").read_bytes()
ROSTERS_2027 = (FIXTURES / "teams-roster-y2027-2026-11-01.json").read_bytes()

TEAMS_URL = f"{BASE_URL}{TEAMS_PATH}"
RATINGS_URL = f"{BASE_URL}{RATINGS_PATH}"
ROSTERS_URL = f"{BASE_URL}{ROSTERS_PATH}"

type Fetch = Callable[[httpx.Client, ResponseCache, int], Fetched[object]]

ENDPOINTS: list[tuple[Fetch, str, bytes]] = [
    (fetch_teams, TEAMS_URL, TEAMS_2027),
    (fetch_adjusted_ratings, RATINGS_URL, RATINGS_2026),
    (fetch_rosters, ROSTERS_URL, ROSTERS_2027),
]
ENDPOINT_IDS = ["teams", "ratings", "rosters"]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_cbbd_client(SecretStr("test-key")) as c:
        yield c


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "cache", FakeClock(datetime(2026, 11, 9, 12, tzinfo=UTC)))


def _json(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"Content-Type": "application/json"})


def _rating(team_id: int, net: float) -> CbbdRating:
    return CbbdRating(team_id=team_id, net=net, adj_o=100 + net, adj_d=100.0)


# --- every endpoint ---


@respx.mock
@pytest.mark.parametrize(("fetch", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_request_carries_bearer_key_and_season(
    client: httpx.Client, cache: ResponseCache, fetch: Fetch, url: str, body: bytes
) -> None:
    route = respx.get(url, params={"season": "2027"}).mock(return_value=_json(body))

    fetch(client, cache, 2027)

    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-key"


@respx.mock
@pytest.mark.parametrize(("fetch", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_each_fetch_makes_one_request_per_run(
    client: httpx.Client, cache: ResponseCache, fetch: Fetch, url: str, body: bytes
) -> None:
    route = respx.get(url).mock(return_value=_json(body))

    first = fetch(client, cache, 2027)
    second = fetch(client, cache, 2027)

    assert route.call_count == 1
    assert second == first


@respx.mock
@pytest.mark.parametrize(("fetch", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_401_is_a_cbbd_source_failure(
    client: httpx.Client, cache: ResponseCache, fetch: Fetch, url: str, body: bytes
) -> None:
    route = respx.get(url).mock(return_value=_json(b'{"message":"Unauthorized"}', status=401))

    result = fetch(client, cache, 2027)

    assert result == SourceFailure("cbbd", "HTTP 401")
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(("fetch", "url", "body"), ENDPOINTS, ids=ENDPOINT_IDS)
def test_a_body_that_is_not_a_list_is_a_failure(
    client: httpx.Client, cache: ResponseCache, fetch: Fetch, url: str, body: bytes
) -> None:
    respx.get(url).mock(return_value=_json(b'{"teams": []}'))

    result = fetch(client, cache, 2027)

    assert isinstance(result, SourceFailure)
    assert result.source == "cbbd"
    assert result.reason.startswith("invalid response: ")


# --- teams ---


@respx.mock
def test_teams_fixture_parses_to_typed_teams(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(TEAMS_URL).mock(return_value=_json(TEAMS_2027))

    teams = fetch_teams(client, cache, 2027)

    assert isinstance(teams, list)
    assert len(teams) == 365
    assert len({team.id for team in teams}) == 365
    assert teams[0] == CbbdTeam(id=264, school="Siena", display_name="Siena Saints", espn_id="2561")


@respx.mock
def test_team_missing_a_kept_field_is_a_failure(client: httpx.Client, cache: ResponseCache) -> None:
    teams = json.loads(TEAMS_2027)
    del teams[0]["school"]
    respx.get(TEAMS_URL).mock(return_value=_json(json.dumps(teams).encode()))

    result = fetch_teams(client, cache, 2027)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("invalid response: ")


# --- adjusted ratings ---


@respx.mock
def test_ratings_fixture_parses_to_typed_ratings(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(RATINGS_URL).mock(return_value=_json(RATINGS_2026))

    ratings = fetch_adjusted_ratings(client, cache, 2026)

    assert isinstance(ratings, list)
    assert len(ratings) == 365
    assert ratings[0] == CbbdRating(team_id=170, net=41.9, adj_o=130.6, adj_d=88.8)


@respx.mock
def test_ratings_before_the_first_games_are_empty(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(RATINGS_URL).mock(return_value=_json(RATINGS_2027))

    ratings = fetch_adjusted_ratings(client, cache, 2027)

    assert ratings == []
    assert rank_ratings([]) == []


@respx.mock
def test_rank_ratings_differs_from_cbbd_net_rank_only_at_ties(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(RATINGS_URL).mock(return_value=_json(RATINGS_2026))
    ratings = fetch_adjusted_ratings(client, cache, 2026)
    assert isinstance(ratings, list)
    # CBBD's own rank gives tied teams the same number (61, 61, 63); spec §3 does not use it.
    cbbd_rank = {row["teamId"]: row["rankings"]["net"] for row in json.loads(RATINGS_2026)}
    tied = Counter(rating.net for rating in ratings)

    ranked = rank_ratings(ratings)

    assert [entry.rank for entry in ranked] == list(range(1, 366))
    for entry in ranked:
        first = cbbd_rank[entry.rating.team_id]
        assert first <= entry.rank < first + tied[entry.rating.net]
    assert sum(entry.rank != cbbd_rank[entry.rating.team_id] for entry in ranked) == 96


def test_rank_ratings_orders_by_net_descending() -> None:
    low, high, mid = _rating(1, -3.5), _rating(2, 20.0), _rating(3, 4.2)

    ranked = rank_ratings([low, high, mid])

    assert ranked == [RankedRating(1, high), RankedRating(2, mid), RankedRating(3, low)]


def test_rank_ratings_ties_keep_cbbd_order() -> None:
    first, second, top = _rating(10, 5.0), _rating(11, 5.0), _rating(12, 9.0)

    ranked = rank_ratings([first, second, top])

    assert [(entry.rank, entry.rating.team_id) for entry in ranked] == [(1, 12), (2, 10), (3, 11)]


# --- rosters ---


@respx.mock
def test_rosters_fixture_parses_to_typed_players(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(ROSTERS_URL).mock(return_value=_json(ROSTERS_2027))

    players = fetch_rosters(client, cache, 2027)

    assert isinstance(players, list)
    assert len(players) == 90
    # The six D-I sample teams; the three non-D-I entries have no players and so no rows.
    assert len({player.team_id for player in players}) == 6
    assert players[0] == CbbdRosterPlayer(team_id=72, name="Caleb Foster")


@respx.mock
def test_roster_player_missing_a_name_is_a_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    rosters = json.loads(ROSTERS_2027)
    del rosters[0]["players"][0]["name"]
    respx.get(ROSTERS_URL).mock(return_value=_json(json.dumps(rosters).encode()))

    result = fetch_rosters(client, cache, 2027)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("invalid response: ")
