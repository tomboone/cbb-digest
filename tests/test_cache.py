from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cbb_digest.sources.cache import CachedResponse, ResponseCache
from tests.fakes import FakeClock

START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _response(body: bytes = b"a,b\n1,2\n", content_type: str = "text/csv") -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"Content-Type": content_type},
        request=httpx.Request("GET", "https://barttorvik.com/getadvstats.php?year=2027&csv=1"),
    )


class CountingFetch:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.calls = 0
        self.response = response or _response()

    def __call__(self) -> httpx.Response:
        self.calls += 1
        return self.response


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def cache(tmp_path: Path, clock: FakeClock) -> ResponseCache:
    return ResponseCache(tmp_path / "cache", clock)


def test_miss_fetches_and_stores_body_and_metadata(cache: ResponseCache, tmp_path: Path) -> None:
    fetch = CountingFetch()

    cached = cache.get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 1
    assert cached == CachedResponse(
        body=b"a,b\n1,2\n",
        status=200,
        content_type="text/csv",
        url="https://barttorvik.com/getadvstats.php?year=2027&csv=1",
        fetched_at=START,
    )
    assert (tmp_path / "cache" / "torvik" / "season-2027").is_file()


def test_second_call_within_ttl_makes_no_request(cache: ResponseCache, clock: FakeClock) -> None:
    fetch = CountingFetch()
    first = cache.get_or_fetch("torvik", "season-2027", fetch)

    clock.advance(timedelta(hours=23, minutes=59))
    second = cache.get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 1
    assert second == first


def test_call_after_ttl_refetches(cache: ResponseCache, clock: FakeClock) -> None:
    fetch = CountingFetch()
    cache.get_or_fetch("torvik", "season-2027", fetch)

    clock.advance(timedelta(hours=24))
    fetch.response = _response(b"a,b\n3,4\n")
    refreshed = cache.get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 2
    assert refreshed.body == b"a,b\n3,4\n"
    assert refreshed.fetched_at == START + timedelta(hours=24)


def test_entry_survives_a_new_cache_instance(tmp_path: Path, clock: FakeClock) -> None:
    fetch = CountingFetch()
    ResponseCache(tmp_path / "cache", clock).get_or_fetch("torvik", "season-2027", fetch)

    ResponseCache(tmp_path / "cache", clock).get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 1


def test_keys_are_independent(cache: ResponseCache) -> None:
    fetch = CountingFetch()

    cache.get_or_fetch("espn", "scoreboard-20261109", fetch)
    cache.get_or_fetch("espn", "scoreboard-20261110", fetch)
    cache.get_or_fetch("torvik", "scoreboard-20261109", fetch)

    assert fetch.calls == 3


def test_binary_body_round_trips(cache: ResponseCache) -> None:
    body = bytes(range(256)) + b"\n\n"
    fetch = CountingFetch(_response(body, "application/octet-stream"))
    cache.get_or_fetch("torvik", "blob", fetch)

    assert cache.get_or_fetch("torvik", "blob", fetch).body == body
    assert fetch.calls == 1


def test_fetch_error_stores_nothing(cache: ResponseCache) -> None:
    def failing() -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        cache.get_or_fetch("torvik", "season-2027", failing)

    fetch = CountingFetch()
    cache.get_or_fetch("torvik", "season-2027", fetch)
    assert fetch.calls == 1


def test_evict_forces_a_refetch(cache: ResponseCache) -> None:
    fetch = CountingFetch()
    cache.get_or_fetch("torvik", "season-2027", fetch)

    cache.evict("torvik", "season-2027")
    cache.evict("torvik", "never-stored")
    cache.get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 2


def test_unreadable_entry_is_refetched(cache: ResponseCache, tmp_path: Path) -> None:
    path = tmp_path / "cache" / "torvik" / "season-2027"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a cache entry")
    fetch = CountingFetch()

    cached = cache.get_or_fetch("torvik", "season-2027", fetch)

    assert fetch.calls == 1
    assert cached.body == b"a,b\n1,2\n"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../escape", ".hidden"])
def test_rejects_unsafe_names(cache: ResponseCache, bad: str) -> None:
    with pytest.raises(ValueError, match="cache"):
        cache.get_or_fetch("torvik", bad, CountingFetch())
    with pytest.raises(ValueError, match="cache"):
        cache.get_or_fetch(bad, "key", CountingFetch())
