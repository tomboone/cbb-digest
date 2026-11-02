"""Every source one week needs, through the 24 h cache (spec §6, §9.1).

`fetch` and `load` read the same sources the same way, so both go through `collect`. `fetch`
hands it real clients, and so fills the cache. `load` hands it `cache_only_clients()`, whose
every request fails, so it reads the cache and nothing else: a response that is missing or
older than 24 h is a `SourceFailure`, never a network call (spec §9.1, "steps after `fetch` read
only from the cache and the database").

Per run that is the Torvik season file and game logs, CBBD's adjusted ratings, and seven ESPN
scoreboard days: 10 requests. CBBD's teams add one more, only when asked for.
"""

from collections.abc import Callable, Sized
from dataclasses import dataclass
from datetime import date

import httpx
from pydantic import SecretStr

from cbb_digest.sources import cbbd, espn, torvik
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.sources.models.cbbd_rating import CbbdRating
from cbb_digest.sources.models.cbbd_team import CbbdTeam
from cbb_digest.sources.models.espn_scoreboard import EspnGame
from cbb_digest.sources.models.torvik_player_game import TorvikPlayerGame
from cbb_digest.sources.models.torvik_player_season import TorvikPlayerSeason


@dataclass(frozen=True, slots=True)
class Clients:
    torvik: httpx.Client
    cbbd: httpx.Client
    espn: httpx.Client

    def all(self) -> tuple[httpx.Client, ...]:
        return (self.torvik, self.cbbd, self.espn)

    def close(self) -> None:
        for client in self.all():
            client.close()


@dataclass(frozen=True, slots=True)
class Sources:
    player_season: Fetched[list[TorvikPlayerSeason]]
    player_games: Fetched[list[TorvikPlayerGame]]
    ratings: Fetched[list[CbbdRating]]
    espn_week: Fetched[list[EspnGame]]
    # `None` when CBBD's teams were not asked for.
    cbbd_teams: Fetched[list[CbbdTeam]] | None

    def outcomes(self) -> list[tuple[str, Fetched[Sized]]]:
        """Each source's label and result, in fetch order; teams only when asked for."""
        outcomes: list[tuple[str, Fetched[Sized]]] = [
            ("torvik player season", self.player_season),
            ("torvik game logs", self.player_games),
            ("cbbd ratings", self.ratings),
        ]
        if self.cbbd_teams is not None:
            outcomes.append(("cbbd teams", self.cbbd_teams))
        outcomes.append(("espn week", self.espn_week))
        return outcomes


class RequestCounter:
    """Counts requests on the wire, retries included; a cache hit sends none."""

    def __init__(self) -> None:
        self.count = 0

    def record(self, request: httpx.Request) -> None:
        self.count += 1


def live_clients(cbbd_api_key: SecretStr, counter: RequestCounter) -> Clients:
    return Clients(
        torvik=torvik.make_torvik_client(counter.record),
        cbbd=cbbd.make_cbbd_client(cbbd_api_key, counter.record),
        espn=espn.make_espn_client(counter.record),
    )


def cache_only_clients() -> Clients:
    """Clients that never reach the network: every request fails, so only the cache answers."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not in the cache: run `cbb-digest fetch` first", request=request)

    def client() -> httpx.Client:
        return httpx.Client(
            base_url="http://cache-only.invalid", transport=httpx.MockTransport(refuse)
        )

    return Clients(torvik=client(), cbbd=client(), espn=client())


def collect(
    clients: Clients,
    cache: ResponseCache,
    *,
    season: int,
    this_week_start: date,
    include_teams: bool,
) -> Sources:
    """Every source for `season` and the week from `this_week_start`; CBBD's teams too when
    `include_teams`. Each source succeeds or fails on its own (spec §9.3)."""
    return Sources(
        player_season=torvik.fetch_player_season(clients.torvik, cache, season),
        player_games=torvik.fetch_player_games(clients.torvik, cache, season),
        ratings=cbbd.fetch_adjusted_ratings(clients.cbbd, cache, season),
        cbbd_teams=cbbd.fetch_teams(clients.cbbd, cache, season) if include_teams else None,
        espn_week=espn.fetch_week(clients.espn, cache, this_week_start),
    )


def refetch_failed(
    clients: Clients,
    cache: ResponseCache,
    sources: Sources,
    *,
    season: int,
    this_week_start: date,
) -> Sources:
    """`sources` with every failed source fetched once more and the rest kept untouched, so a
    source that answered is never asked twice (spec §9.3).

    The refetch reaches the network: a failure always evicts its cache entry (`fetch_source`).
    The only entries it reads are the ESPN days this run already fetched before a later day
    failed.
    """
    return Sources(
        player_season=_again(
            sources.player_season,
            lambda: torvik.fetch_player_season(clients.torvik, cache, season),
        ),
        player_games=_again(
            sources.player_games,
            lambda: torvik.fetch_player_games(clients.torvik, cache, season),
        ),
        ratings=_again(
            sources.ratings, lambda: cbbd.fetch_adjusted_ratings(clients.cbbd, cache, season)
        ),
        cbbd_teams=(
            None
            if sources.cbbd_teams is None
            else _again(sources.cbbd_teams, lambda: cbbd.fetch_teams(clients.cbbd, cache, season))
        ),
        espn_week=_again(
            sources.espn_week, lambda: espn.fetch_week(clients.espn, cache, this_week_start)
        ),
    )


def _again[T](result: Fetched[T], fetch: Callable[[], Fetched[T]]) -> Fetched[T]:
    return fetch() if isinstance(result, SourceFailure) else result


def teams_cached(cache: ResponseCache, season: int) -> bool:
    """Whether a `fetch` stored CBBD's teams for `season`, so `load` should store them."""
    return cache.peek(cbbd.SOURCE, cbbd.teams_cache_key(season)) is not None
