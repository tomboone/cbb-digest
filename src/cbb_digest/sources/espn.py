"""ESPN client: this week's games, with tip times, venues and TV networks (spec §6, §8.3).

ESPN's unofficial scoreboard is the only source that carries the network. It takes one US-local
day per request: a date range is rejected with HTTP 400, and a whole month is cut off at `limit`
(probe NEU-2093). So a week is seven daily requests, each through the shared client and its own
24 h cache entry. `groups=50` is all of Division I, including D-I teams' non-D-I opponents.

Any day that fails fails the week: a schedule missing a day is never served (spec §9.3). The days
fetched before the failure stay cached, so the retry refetches only the rest.
"""

from collections.abc import Callable
from datetime import date, timedelta

import httpx

from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, SourceFailure, fetch_source
from cbb_digest.sources.http import make_client
from cbb_digest.sources.models.espn_scoreboard import EspnGame, parse_scoreboard

BASE_URL = "https://site.api.espn.com"
SOURCE = "espn"
SCOREBOARD_PATH = "/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
# A day's slate is a few hundred games at most; the probe's 1,000 leaves no chance of a cut-off.
SCOREBOARD_PARAMS = {"groups": "50", "limit": "1000"}
DAYS_PER_WEEK = 7


def make_espn_client(on_request: Callable[[httpx.Request], None] | None = None) -> httpx.Client:
    return make_client(BASE_URL, on_request=on_request)


def fetch_week(client: httpx.Client, cache: ResponseCache, start: date) -> Fetched[list[EspnGame]]:
    """Every game on the seven US-local days from `start`, in day order, then ESPN's order."""
    games: list[EspnGame] = []
    for offset in range(DAYS_PER_WEEK):
        day = _fetch_day(client, cache, start + timedelta(days=offset))
        if isinstance(day, SourceFailure):
            return day
        games.extend(day)
    return games


def _fetch_day(client: httpx.Client, cache: ResponseCache, day: date) -> Fetched[list[EspnGame]]:
    dates = day.strftime("%Y%m%d")
    return fetch_source(
        SOURCE,
        f"scoreboard-{dates}",
        lambda: client.get(SCOREBOARD_PATH, params={"dates": dates, **SCOREBOARD_PARAMS}),
        parse_scoreboard,
        cache,
    )
