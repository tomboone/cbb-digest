"""CollegeBasketballData client: D-I teams, adjusted ratings and rosters (spec §6, ADR-0002).

Each fetch is one GET with `Authorization: Bearer <CBBD_API_KEY>`, through the shared client and
24 h cache, so a re-run inside the window costs no quota (1,000 calls on the free key). A run reads
the ratings; teams only when the crosswalk is refreshed, and rosters only in reset mode.

The key is configuration, not data: a missing or blank key stops the app at startup (`Settings`),
so it never surfaces as a 401 source failure. A 401 from a key CBBD has revoked is a source
failure like any other HTTP error.
"""

import httpx
from pydantic import SecretStr

from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, fetch_source
from cbb_digest.sources.http import make_client
from cbb_digest.sources.models.cbbd_rating import CbbdRating, parse_ratings
from cbb_digest.sources.models.cbbd_roster import CbbdRosterPlayer, parse_rosters
from cbb_digest.sources.models.cbbd_team import CbbdTeam, parse_teams

BASE_URL = "https://api.collegebasketballdata.com"
SOURCE = "cbbd"
TEAMS_PATH = "/teams"
RATINGS_PATH = "/ratings/adjusted"
ROSTERS_PATH = "/teams/roster"


def make_cbbd_client(api_key: SecretStr) -> httpx.Client:
    return make_client(BASE_URL, headers={"Authorization": f"Bearer {api_key.get_secret_value()}"})


def fetch_teams(client: httpx.Client, cache: ResponseCache, season: int) -> Fetched[list[CbbdTeam]]:
    """Every D-I team for `season`, the seed of the canonical teams (spec §7.5)."""
    return fetch_source(
        SOURCE,
        f"teams-{season}",
        lambda: client.get(TEAMS_PATH, params={"season": str(season)}),
        parse_teams,
        cache,
    )


def fetch_adjusted_ratings(
    client: httpx.Client, cache: ResponseCache, season: int
) -> Fetched[list[CbbdRating]]:
    """Each D-I team's adjusted ratings; `[]` until the season's first games are played."""
    return fetch_source(
        SOURCE,
        f"ratings-adjusted-{season}",
        lambda: client.get(RATINGS_PATH, params={"season": str(season)}),
        parse_ratings,
        cache,
    )


def fetch_rosters(
    client: httpx.Client, cache: ResponseCache, season: int
) -> Fetched[list[CbbdRosterPlayer]]:
    """Every D-I player on a `season` roster, in one request (reset mode, spec §11)."""
    return fetch_source(
        SOURCE,
        f"rosters-{season}",
        lambda: client.get(ROSTERS_PATH, params={"season": str(season)}),
        parse_rosters,
        cache,
    )
