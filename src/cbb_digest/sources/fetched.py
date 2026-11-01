"""A source's validated value, or the reason it failed (spec §6, §9.3).

Every way a source can go wrong — an HTTP error that outlived the retries, a transport error,
Torvik's browser check, or a body that no longer matches its model — comes back as a
`SourceFailure`, so a dead or reshaped source degrades the digest instead of crashing the run.

There is no snapshot fallback, unlike nba-today: stale weekly data is never substituted for a
failed source (spec §9.3). The caller retries later; only fresh, valid responses are cached.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from cbb_digest.sources.cache import ResponseCache

# The marker Torvik's JavaScript browser check carries; it is served as HTTP 200 (spec §6).
CHALLENGE_MARKER = b"Verifying Browser"

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceFailure:
    source: str
    reason: str


type Fetched[T] = T | SourceFailure


class _Rejected(Exception):
    """A response not worth caching; carries the failure to report."""

    def __init__(self, failure: SourceFailure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


def detect_challenge(source: str, response: httpx.Response) -> SourceFailure | None:
    """A failure if the response is a bot check rather than data.

    Every endpoint the pipeline reads serves CSV or JSON, so HTML is a challenge whatever the
    status. A challenge is reported, never retried or worked around (ADR-0002).
    """
    content_type = response.headers.get("Content-Type", "")
    if content_type.split(";")[0].strip().lower() == "text/html":
        return SourceFailure(source, f"challenge: {content_type} response")
    if CHALLENGE_MARKER in response.content:
        return SourceFailure(source, f"challenge: body contains {CHALLENGE_MARKER.decode()!r}")
    return None


def fetch_source[T](
    source: str,
    key: str,
    request: Callable[[], httpx.Response],
    parse: Callable[[bytes], T],
    cache: ResponseCache,
) -> Fetched[T]:
    """Fetch through the 24 h cache and parse the body, or say why the source failed.

    `request` makes one call on a `make_client` client. Only a 2xx response that is not a
    challenge is cached. A failed fetch also evicts any expired entry under the key, so nothing
    reading the cache later can mistake last week's body for this week's (spec §9.3).

    `parse` validates the body, typically through a pydantic model or adapter. A shape change
    surfaces as `ValueError` (`pydantic.ValidationError` is one), or as `LookupError`/`TypeError`
    from a positional parser. Either way the source fails and the stored body is evicted, so the
    retry refetches instead of re-reading it.

    A source whose non-2xx answer carries meaning (Torvik's 302 for an unpublished game log) maps
    it to an ordinary response inside `request`.
    """

    def fetch() -> httpx.Response:
        response = request()
        if not response.is_success:
            raise _Rejected(SourceFailure(source, f"HTTP {response.status_code}"))
        challenge = detect_challenge(source, response)
        if challenge is not None:
            raise _Rejected(challenge)
        return response

    try:
        cached = cache.get_or_fetch(source, key, fetch)
    except _Rejected as exc:
        cache.evict(source, key)
        return _failed(exc.failure)
    except httpx.HTTPError as exc:
        cache.evict(source, key)
        return _failed(SourceFailure(source, f"{type(exc).__name__}: {exc}"))

    try:
        return parse(cached.body)
    except (ValueError, LookupError, TypeError) as exc:
        cache.evict(source, key)
        log.error("source payload did not parse", extra={"source": source}, exc_info=True)
        return SourceFailure(source, f"invalid response: {_first_line(exc)}")


def _failed(failure: SourceFailure) -> SourceFailure:
    log.warning("source failed", extra={"source": failure.source, "reason": failure.reason})
    return failure


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
