import csv
import gzip
import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from cbb_digest.sources import http
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import SourceFailure
from cbb_digest.sources.models.torvik_player_game import TorvikPlayerGame
from cbb_digest.sources.models.torvik_player_season import TorvikPlayerSeason
from cbb_digest.sources.torvik import (
    BASE_URL,
    PLAYER_SEASON_PATH,
    fetch_player_games,
    fetch_player_season,
    make_torvik_client,
    player_games_path,
)
from tests.fakes import FakeClock

FIXTURES = Path(__file__).parent / "fixtures" / "torvik"
SEASON_2026 = (FIXTURES / "getadvstats-y2026-2026-11-01.csv").read_bytes()
SEASON_2027 = (FIXTURES / "getadvstats-y2027-2026-11-01.csv").read_bytes()
GAMES_2026 = (FIXTURES / "all_advgames-y2026-2026-11-01.json").read_bytes()
CHALLENGE_HTML = (FIXTURES / "playerstat-challenge-2026-11-01.html").read_bytes()

SEASON_URL = f"{BASE_URL}{PLAYER_SEASON_PATH}"
GAMES_URL = f"{BASE_URL}{player_games_path(2026)}"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_torvik_client() as c:
        yield c


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "cache", FakeClock(datetime(2026, 11, 9, 12, tzinfo=UTC)))


def _csv(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "text/csv"})


def _json(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})


def _drop_csv_field(body: bytes, index: int) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    for row in csv.reader(io.StringIO(body.decode())):
        writer.writerow(row[:index] + row[index + 1 :])
    return out.getvalue().encode()


# --- player season file ---


@respx.mock
def test_player_season_fixture_parses_to_typed_rows(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(SEASON_URL, params={"year": "2026", "csv": "1"}).mock(
        return_value=_csv(SEASON_2026)
    )

    rows = fetch_player_season(client, cache, 2026)

    assert route.call_count == 1
    assert isinstance(rows, list)
    assert len(rows) == 440
    assert {row.season for row in rows} == {2026}
    assert {row.class_year for row in rows} == {"Fr", "So", "Jr", "Sr"}
    assert len({row.team for row in rows}) == 365


@respx.mock
def test_player_season_row_maps_each_column(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(SEASON_URL).mock(return_value=_csv(SEASON_2027))

    rows = fetch_player_season(client, cache, 2027)

    assert isinstance(rows, list)
    assert rows[0] == TorvikPlayerSeason(
        pid=75474,
        season=2027,
        name="Logan Duncomb",
        team="Notre Dame",
        class_year="Sr",
        games=1,
        min_per_game=20.9,
        pts_per_game=9,
        reb_per_game=3,
        ast_per_game=4,
        fg2m=3,
        fg2a=5,
        fg3m=1,
        fg3a=2,
        fta=2,
    )
    assert (rows[0].fgm, rows[0].fga) == (4, 7)


@respx.mock
def test_player_season_with_a_column_removed_is_a_shape_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(SEASON_URL).mock(return_value=_csv(_drop_csv_field(SEASON_2027, 30)))

    result = fetch_player_season(client, cache, 2027)

    assert isinstance(result, SourceFailure)
    assert result.source == "torvik"
    assert "shape: row 1 has 66 fields, expected 67" in result.reason


@respx.mock
def test_player_season_unknown_class_is_a_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(SEASON_URL).mock(return_value=_csv(SEASON_2027.replace(b",Sr,", b",Gr,", 1)))

    result = fetch_player_season(client, cache, 2027)

    assert isinstance(result, SourceFailure)
    assert result.reason.startswith("invalid response: ")


@respx.mock
def test_player_season_blank_class_is_unknown(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(SEASON_URL).mock(return_value=_csv(SEASON_2027.replace(b",Sr,", b",,", 1)))

    rows = fetch_player_season(client, cache, 2027)

    assert isinstance(rows, list)
    assert rows[0].class_year is None


@respx.mock
def test_player_season_empty_file_is_no_rows(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(SEASON_URL).mock(return_value=_csv(b""))

    assert fetch_player_season(client, cache, 2027) == []


@respx.mock
def test_player_season_challenge_fails_after_exactly_one_request(
    client: httpx.Client, cache: ResponseCache
) -> None:
    # The challenge is served as HTTP 200, and with a CSV content type it is caught by its body.
    route = respx.get(SEASON_URL).mock(return_value=_csv(CHALLENGE_HTML))

    result = fetch_player_season(client, cache, 2027)

    assert route.call_count == 1
    assert isinstance(result, SourceFailure)
    assert result.source == "torvik"
    assert "challenge" in result.reason


# --- player game logs ---


@respx.mock
def test_player_games_fixture_parses_to_typed_rows(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(GAMES_URL).mock(return_value=_json(GAMES_2026))

    rows = fetch_player_games(client, cache, 2026)

    assert route.call_count == 1
    assert isinstance(rows, list)
    assert len(rows) == 2074
    assert {row.team for row in rows} == {
        "Iowa",
        "Duke",
        "Notre Dame",
        "Villanova",
        "Tennessee",
        "Kentucky",
    }
    assert all(row.min > 0 for row in rows)


@respx.mock
def test_player_game_row_maps_each_column(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(GAMES_URL).mock(return_value=_json(GAMES_2026))

    rows = fetch_player_games(client, cache, 2026)

    assert isinstance(rows, list)
    second = rows[1]
    assert second == TorvikPlayerGame(
        pid=72678,
        game_date=date(2025, 11, 8),
        team="Villanova",
        opponent="Queens",
        min=20.7,
        pts=5,
        orb=0,
        drb=0,
        ast=4,
        fg2m=2,
        fg2a=4,
        fg3m=0,
        fg3a=1,
        fta=2,
    )
    assert (second.fgm, second.fga, second.reb) == (2, 5, 0)


@respx.mock
def test_player_games_with_a_column_removed_is_a_shape_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    rows = json.loads(GAMES_2026)
    respx.get(GAMES_URL).mock(return_value=_json(json.dumps([row[:-1] for row in rows]).encode()))

    result = fetch_player_games(client, cache, 2026)

    assert isinstance(result, SourceFailure)
    assert result.source == "torvik"
    assert "shape: row 1 has 52 fields, expected 53" in result.reason


@respx.mock
def test_player_games_body_that_is_not_a_list_is_a_shape_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(GAMES_URL).mock(return_value=_json(b'{"rows": []}'))

    result = fetch_player_games(client, cache, 2026)

    assert isinstance(result, SourceFailure)
    assert "shape: body is dict" in result.reason


@respx.mock
def test_player_games_challenge_fails_after_exactly_one_request(
    client: httpx.Client, cache: ResponseCache
) -> None:
    route = respx.get(GAMES_URL).mock(
        return_value=httpx.Response(
            200, content=CHALLENGE_HTML, headers={"Content-Type": "text/html"}
        )
    )

    result = fetch_player_games(client, cache, 2026)

    assert route.call_count == 1
    assert isinstance(result, SourceFailure)
    assert "challenge" in result.reason


@respx.mock
@pytest.mark.parametrize("location", ["/", f"{BASE_URL}/"])
def test_unpublished_game_logs_redirect_home_is_an_empty_log(
    client: httpx.Client, cache: ResponseCache, location: str
) -> None:
    route = respx.get(GAMES_URL).mock(
        return_value=httpx.Response(302, headers={"Location": location})
    )

    assert fetch_player_games(client, cache, 2026) == []
    assert route.call_count == 1


@respx.mock
def test_any_other_game_log_redirect_is_a_failure(
    client: httpx.Client, cache: ResponseCache
) -> None:
    respx.get(GAMES_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "/elsewhere.json.gz"})
    )

    result = fetch_player_games(client, cache, 2026)

    assert result == SourceFailure("torvik", "HTTP 302")


@respx.mock
def test_gzip_encoded_game_logs_are_decoded(client: httpx.Client, cache: ResponseCache) -> None:
    respx.get(GAMES_URL).mock(
        return_value=httpx.Response(
            200,
            content=gzip.compress(GAMES_2026),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
    )

    rows = fetch_player_games(client, cache, 2026)

    assert isinstance(rows, list)
    assert len(rows) == 2074
