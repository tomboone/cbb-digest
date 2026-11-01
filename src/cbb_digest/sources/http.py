"""The one way sources build an HTTP client: timeout, retries with backoff, User-Agent (spec §6).

Ported from nba-today. Two changes: the User-Agent is a desktop browser's, as spec §6 asks and as
the source probe (NEU-2093) used, and `headers` carries per-source extras such as CBBD's bearer
token. A User-Agent is not a way past a bot check: a challenge is still a source failure, and is
never retried or worked around (ADR-0002, `fetched.detect_challenge`).
Redirects are never followed (httpx's default): Torvik 302s an unpublished game log to `/`, and
the source client must see that redirect rather than the home page.
"""

import logging
import time
from collections.abc import Mapping
from typing import override

import httpx

TIMEOUT_SECONDS = 20.0
RETRIES = 3
BACKOFF_SECONDS = 0.5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

log = logging.getLogger(__name__)


class RetryTransport(httpx.BaseTransport):
    """Retry connect errors and 5xx responses, sleeping `backoff * 2**attempt` between tries.

    After the last retry a 5xx response is returned as-is and a connect error is re-raised,
    so the caller sees the same outcome it would have without retries.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        retries: int = RETRIES,
        backoff: float = BACKOFF_SECONDS,
    ) -> None:
        self._inner = inner
        self._retries = retries
        self._backoff = backoff

    @override
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                response = self._inner.handle_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if last:
                    raise
                reason = type(exc).__name__
            else:
                if response.status_code < 500 or last:
                    return response
                response.close()
                reason = str(response.status_code)
            delay = self._backoff * 2**attempt
            log.warning(
                "retrying request",
                extra={"url": str(request.url), "reason": reason, "attempt": attempt + 1},
            )
            time.sleep(delay)
        raise AssertionError("unreachable")

    @override
    def close(self) -> None:
        self._inner.close()


def make_client(
    base_url: str,
    *,
    user_agent: str = USER_AGENT,
    headers: Mapping[str, str] | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"User-Agent": user_agent, **(headers or {})},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=RetryTransport(httpx.HTTPTransport()),
    )
