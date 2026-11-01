import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from cbb_digest.sources import http
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.espn import BASE_URL, SCOREBOARD_PATH, fetch_week, make_espn_client
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.sources.models.espn_scoreboard import EspnGame, EspnTeam
from tests.fakes import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "espn"
# The ESPN schedule for Tuesday 2026-11-03 ET: 29 games.
TUESDAY = (FIXTURES / "scoreboard-2026-11-01.json").read_bytes()
TUESDAY_DATE = "20261103"
# The Monday that opens the week holding the fixture's Tuesday.
MONDAY = date(2026, 11, 2)
WEEK_DATES = [f"202611{day:02d}" for day in range(2, 9)]
NO_GAMES = b'{"events": []}'

SCOREBOARD_URL = f"{BASE_URL}{SCOREBOARD_PATH}"

# An in-place edit of one raw event from the fixture.
type Edit = Callable[[dict[str, Any]], object]


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_espn_client() as c:
        yield c


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "cache", FakeClock(datetime(2026, 11, 2, 12, tzinfo=UTC)))


def _json(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"Content-Type": "application/json"})


def _serving(tuesday: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """A scoreboard handler: `tuesday` for the fixture's day, no games on every other day."""

    def respond(request: httpx.Request) -> httpx.Response:
        return _json(tuesday if request.url.params["dates"] == TUESDAY_DATE else NO_GAMES)

    return respond


_tuesday_only = _serving(TUESDAY)


def _edited(edit: Edit) -> bytes:
    """The fixture with `edit` applied to its first event."""
    payload = json.loads(TUESDAY)
    edit(payload["events"][0])
    return json.dumps(payload).encode()


def _week_of(body: bytes, client: httpx.Client, cache: ResponseCache) -> Fetched[list[EspnGame]]:
    """`fetch_week` with `body` as the Tuesday scoreboard."""
    respx.get(SCOREBOARD_URL).mock(side_effect=_serving(body))
    return fetch_week(client, cache, MONDAY)


# --- requests ---


@respx.mock
def test_a_week_is_seven_daily_requests(client: httpx.Client, cache: ResponseCache) -> None:
    # ESPN rejects a date range with HTTP 400 (spec §6), so each day is its own request.
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _tuesday_only(request)

    route = respx.get(SCOREBOARD_URL).mock(side_effect=record)

    fetch_week(client, cache, MONDAY)

    assert route.call_count == 7
    assert [request.url.params["dates"] for request in requests] == WEEK_DATES
    for request in requests:
        assert request.url.params["groups"] == "50"
        assert request.url.params["limit"] == "1000"


@respx.mock
def test_a_rerun_inside_the_window_makes_no_requests(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(SCOREBOARD_URL).mock(side_effect=_tuesday_only)

    first = fetch_week(client, cache, MONDAY)
    second = fetch_week(client, cache, MONDAY)

    assert route.call_count == 7
    assert second == first


@respx.mock
def test_a_failed_day_fails_the_week(client: httpx.Client, cache: ResponseCache) -> None:
    def wednesday_fails(request: httpx.Request) -> httpx.Response:
        if request.url.params["dates"] == "20261104":
            return _json(b'{"code":400,"message":"Failed to get events endpoint."}', status=400)
        return _tuesday_only(request)

    respx.get(SCOREBOARD_URL).mock(side_effect=wednesday_fails)

    result = fetch_week(client, cache, MONDAY)

    assert result == SourceFailure("espn", "HTTP 400")


@respx.mock
def test_the_retry_refetches_only_the_days_not_yet_cached(
    client: httpx.Client, cache: ResponseCache
) -> None:
    failing = respx.get(SCOREBOARD_URL, params={"dates": "20261104"}).mock(
        return_value=_json(b"{}", status=503)
    )
    others = respx.get(SCOREBOARD_URL).mock(side_effect=_tuesday_only)
    assert isinstance(fetch_week(client, cache, MONDAY), SourceFailure)
    assert others.call_count == 2
    failing.mock(side_effect=_tuesday_only)

    games = fetch_week(client, cache, MONDAY)

    assert isinstance(games, list)
    assert len(games) == 29
    # Wednesday's retry, then Thursday through Sunday; Monday and Tuesday come from the cache.
    assert others.call_count == 2 + 4


# --- the scoreboard ---


@respx.mock
def test_fixture_parses_to_games_with_networks(client: httpx.Client, cache: ResponseCache) -> None:
    games = _week_of(TUESDAY, client, cache)

    assert isinstance(games, list)
    assert len(games) == 29
    assert all(isinstance(game, EspnGame) and game.networks for game in games)
    assert games[0] == EspnGame(
        espn_id="401906848",
        start=datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
        home=EspnTeam(espn_id="2633", name="Tennessee", conference_id="23"),
        away=EspnTeam(espn_id="2747", name="Wofford", conference_id="24"),
        venue="Food City Center",
        networks=["SEC Network"],
        status="STATUS_SCHEDULED",
    )


@respx.mock
def test_start_is_utc(client: httpx.Client, cache: ResponseCache) -> None:
    body = _edited(lambda event: event.update(date="2026-11-03T18:00-05:00"))

    games = _week_of(body, client, cache)

    assert isinstance(games, list)
    assert games[0].start == datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
    assert games[0].start.tzinfo is UTC


@respx.mock
def test_a_game_with_no_broadcasts_has_no_networks(
    client: httpx.Client, cache: ResponseCache
) -> None:
    body = _edited(lambda event: event["competitions"][0].pop("broadcasts"))

    games = _week_of(body, client, cache)

    assert isinstance(games, list)
    assert games[0].networks == []


@respx.mock
def test_networks_are_flattened_deduplicated_and_in_order(
    client: httpx.Client, cache: ResponseCache
) -> None:
    broadcasts = [
        {"market": "national", "names": ["ESPN2", "ESPN+"]},
        {"market": "home", "names": ["ESPN+", "SECN+"]},
    ]
    body = _edited(lambda event: event["competitions"][0].update(broadcasts=broadcasts))

    games = _week_of(body, client, cache)

    assert isinstance(games, list)
    assert games[0].networks == ["ESPN2", "ESPN+", "SECN+"]


@respx.mock
def test_missing_optional_fields_are_tolerated(client: httpx.Client, cache: ResponseCache) -> None:
    def strip(event: dict[str, Any]) -> None:
        del event["status"]
        del event["competitions"][0]["venue"]
        for competitor in event["competitions"][0]["competitors"]:
            del competitor["team"]["location"]

    games = _week_of(_edited(strip), client, cache)

    assert isinstance(games, list)
    assert games[0].venue is None
    assert games[0].status is None
    assert games[0].home == EspnTeam(espn_id="2633", name=None, conference_id="23")


@respx.mock
def test_a_team_with_no_conference_is_not_division_one(
    client: httpx.Client, cache: ResponseCache
) -> None:
    body = _edited(
        lambda event: event["competitions"][0]["competitors"][1]["team"].pop("conferenceId")
    )

    games = _week_of(body, client, cache)

    assert isinstance(games, list)
    sides = {games[0].home.is_division_one, games[0].away.is_division_one}
    assert sides == {True, False}


def _drop_team_id(event: dict[str, Any]) -> None:
    del event["competitions"][0]["competitors"][0]["team"]["id"]


def _no_home_team(event: dict[str, Any]) -> None:
    event["competitions"][0]["competitors"][0]["homeAway"] = "away"


LOUD_FAILURES: list[tuple[Edit, str]] = [
    (lambda event: event.pop("id"), "invalid response: 1 validation error"),
    (lambda event: event.pop("date"), "invalid response: 1 validation error"),
    (_drop_team_id, "invalid response: 1 validation error"),
    (lambda event: event.update(competitions=[]), "invalid response: shape: event 401906848"),
    (_no_home_team, "invalid response: shape: event 401906848"),
]


@respx.mock
@pytest.mark.parametrize(
    ("edit", "reason"),
    LOUD_FAILURES,
    ids=["event-id", "start", "team-id", "competition", "home-team"],
)
def test_a_game_missing_an_id_or_its_start_fails_loudly(
    client: httpx.Client, cache: ResponseCache, edit: Edit, reason: str
) -> None:
    result = _week_of(_edited(edit), client, cache)

    assert isinstance(result, SourceFailure)
    assert result.source == "espn"
    assert result.reason.startswith(reason)


@respx.mock
def test_a_payload_missing_events_is_a_shape_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    result = _week_of(b'{"leagues": []}', client, cache)

    assert result == SourceFailure("espn", "invalid response: shape: scoreboard has no events")
