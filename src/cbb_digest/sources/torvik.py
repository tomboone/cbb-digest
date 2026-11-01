"""Torvik (barttorvik.com) client: the player season file and the player game logs (spec §6).

Two plain GETs per run, each through the shared client and 24 h cache. Torvik's browser check is
served as HTTP 200, so `fetch_source` treats any HTML or `Verifying Browser` body as a failure and
never retries it (ADR-0002). No other Torvik endpoint is fetched: `trank.php` and the player pages
are behind that check, and `getgamestats.php` is one row per team-game, not player game logs.

Before a season's game logs are published, their URL 302-redirects to `/`. The client does not
follow redirects, and reads that one as an empty game log; any other redirect is a failure.
"""

import httpx

from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, fetch_source
from cbb_digest.sources.http import make_client
from cbb_digest.sources.models.torvik_player_game import TorvikPlayerGame, parse_player_games
from cbb_digest.sources.models.torvik_player_season import (
    TorvikPlayerSeason,
    parse_player_season,
)

BASE_URL = "https://barttorvik.com"
SOURCE = "torvik"
PLAYER_SEASON_PATH = "/getadvstats.php"


def make_torvik_client() -> httpx.Client:
    return make_client(BASE_URL)


def player_games_path(season: int) -> str:
    return f"/{season}_all_advgames.json.gz"


def fetch_player_season(
    client: httpx.Client, cache: ResponseCache, season: int
) -> Fetched[list[TorvikPlayerSeason]]:
    """Every D-I player's season-to-date line. Rows may belong to another season (see model)."""
    return fetch_source(
        SOURCE,
        f"player-season-{season}",
        lambda: client.get(PLAYER_SEASON_PATH, params={"year": str(season), "csv": "1"}),
        parse_player_season,
        cache,
    )


def fetch_player_games(
    client: httpx.Client, cache: ResponseCache, season: int
) -> Fetched[list[TorvikPlayerGame]]:
    """One row per player per game played this season; `[]` before the file is published."""

    def request() -> httpx.Response:
        response = client.get(player_games_path(season))
        if _is_unpublished(response):
            return httpx.Response(
                200,
                content=b"[]",
                headers={"Content-Type": "application/json"},
                request=response.request,
            )
        return response

    return fetch_source(SOURCE, f"player-games-{season}", request, parse_player_games, cache)


def _is_unpublished(response: httpx.Response) -> bool:
    if not response.is_redirect:
        return False
    home = response.request.url.join("/")
    return response.request.url.join(response.headers.get("Location", "")) == home
