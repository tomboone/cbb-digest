from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import BaseModel, TypeAdapter

from cbb_digest.sources import http
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import SourceFailure, detect_challenge, fetch_source
from cbb_digest.sources.http import make_client
from tests.fakes import FakeClock

BASE = "https://barttorvik.com"
PATH = "/getadvstats.php"
URL = f"{BASE}{PATH}"
FIXTURES = Path(__file__).parent / "fixtures"
CHALLENGE_HTML = (FIXTURES / "torvik" / "playerstat-challenge-2026-11-01.html").read_bytes()


class Team(BaseModel):
    id: int
    school: str


TEAMS = TypeAdapter(list[Team])


def parse_teams(body: bytes) -> list[Team]:
    return TEAMS.validate_json(body)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client(BASE) as c:
        yield c


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 9, 12, tzinfo=UTC))


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> ResponseCache:
    return ResponseCache(tmp_path / "cache", clock)


def _fetch(client: httpx.Client, cache: ResponseCache) -> list[Team] | SourceFailure:
    return fetch_source("cbbd", "teams-2027", lambda: client.get(PATH), parse_teams, cache)


@respx.mock
def test_valid_response_returns_the_parsed_value(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "school": "Iowa"}, {"id": 2, "school": "Duke"}]
        )
    )

    assert _fetch(client, cache) == [Team(id=1, school="Iowa"), Team(id=2, school="Duke")]


@respx.mock
def test_second_fetch_within_the_window_is_served_from_the_cache(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, json=[{"id": 1, "school": "Iowa"}])
    )

    first = _fetch(client, cache)
    second = _fetch(client, cache)

    assert first == second == [Team(id=1, school="Iowa")]
    assert route.call_count == 1


@respx.mock
def test_fetch_after_the_window_requests_again(
    client: httpx.Client, cache: ResponseCache, clock: FakeClock
) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"id": 1, "school": "Iowa"}]),
            httpx.Response(200, json=[{"id": 1, "school": "Iowa Hawkeyes"}]),
        ]
    )

    _fetch(client, cache)
    clock.advance(timedelta(hours=24))
    refreshed = _fetch(client, cache)

    assert refreshed == [Team(id=1, school="Iowa Hawkeyes")]
    assert route.call_count == 2


@respx.mock
def test_failed_refetch_drops_the_expired_entry(
    client: httpx.Client, cache: ResponseCache, clock: FakeClock, tmp_path: Path
) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"id": 1, "school": "Iowa"}]),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
        ]
    )
    _fetch(client, cache)

    clock.advance(timedelta(days=7))
    result = _fetch(client, cache)

    assert result == SourceFailure("cbbd", "HTTP 503")
    assert not (tmp_path / "cache" / "cbbd" / "teams-2027").exists()


@respx.mock
def test_positional_parser_index_error_is_a_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"Stirtz,Iowa\n"))

    def third_field(body: bytes) -> str:
        return body.decode().split(",")[2]

    result = fetch_source("torvik", "season-2027", lambda: client.get(PATH), third_field, cache)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("invalid response")


@respx.mock
def test_html_challenge_on_a_csv_endpoint_fails_after_exactly_one_request(
    client: httpx.Client, cache: ResponseCache, sleeps: list[float]
) -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=CHALLENGE_HTML, headers={"Content-Type": "text/html; charset=UTF-8"}
        )
    )

    result = fetch_source("torvik", "season-2027", lambda: client.get(PATH), bytes, cache)

    assert isinstance(result, SourceFailure)
    assert result.source == "torvik"
    assert "challenge" in result.reason
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_a_challenge_is_not_cached(client: httpx.Client, cache: ResponseCache) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, content=CHALLENGE_HTML, headers={"Content-Type": "text/html"}),
            httpx.Response(200, json=[{"id": 1, "school": "Iowa"}]),
        ]
    )

    assert isinstance(_fetch(client, cache), SourceFailure)
    assert _fetch(client, cache) == [Team(id=1, school="Iowa")]
    assert route.call_count == 2


@respx.mock
def test_500_is_retried_a_bounded_number_of_times_then_fails(
    client: httpx.Client, cache: ResponseCache, sleeps: list[float]
) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))

    result = _fetch(client, cache)

    assert result == SourceFailure("cbbd", "HTTP 500")
    assert route.call_count == 1 + http.RETRIES
    assert len(sleeps) == http.RETRIES


@respx.mock
def test_4xx_fails_without_retry(client: httpx.Client, cache: ResponseCache) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(401, json={"message": "no key"}))

    assert _fetch(client, cache) == SourceFailure("cbbd", "HTTP 401")
    assert route.call_count == 1


@respx.mock
def test_unfollowed_redirect_is_a_failure(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"Location": "/"}))

    assert _fetch(client, cache) == SourceFailure("cbbd", "HTTP 302")


@respx.mock
def test_transport_error_is_a_failure(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))

    result = _fetch(client, cache)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("ReadTimeout")


@respx.mock
def test_shape_change_is_a_failure_and_is_evicted(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=[{"teamId": 1, "name": "Iowa"}]),
            httpx.Response(200, json=[{"id": 1, "school": "Iowa"}]),
        ]
    )

    first = _fetch(client, cache)
    second = _fetch(client, cache)

    assert isinstance(first, SourceFailure)
    assert first.reason.startswith("invalid response")
    assert second == [Team(id=1, school="Iowa")]
    assert route.call_count == 2


@respx.mock
def test_body_that_is_not_json_is_a_failure(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"\x1f\x8b garbage"))

    result = _fetch(client, cache)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("invalid response")


def _response(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": content_type})


def test_detects_html_content_type() -> None:
    failure = detect_challenge("torvik", _response(b"<html></html>", "text/html; charset=utf-8"))

    assert failure is not None
    assert "challenge" in failure.reason
    assert "text/html" in failure.reason


def test_detects_challenge_markup_whatever_the_content_type() -> None:
    failure = detect_challenge("torvik", _response(CHALLENGE_HTML, "text/csv"))

    assert failure is not None
    assert "Verifying Browser" in failure.reason


@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"Stirtz,Iowa,B10\n", "text/csv"), (b"[]", "application/json"), (b"", "")],
)
def test_ordinary_responses_are_not_challenges(body: bytes, content_type: str) -> None:
    assert detect_challenge("torvik", _response(body, content_type)) is None
