"""The healthchecks.io client, ported from nba-today, and the run-status mapping (spec §3, §10).

Not the `serve` loop's liveness row: that is `liveness.py`. This heartbeat is an outbound ping.

Every scheduled week ends in one ping. `sent`, `sent_degraded`, `skipped_offseason` and
`skipped_missed` ping success, so the check hears from every Monday, in season or out, and never
needs pausing; `failed` pings `/fail` with the reason.
"""

import logging
from collections.abc import Sequence

import httpx

from cbb_digest.runs import FinalStatus

TIMEOUT_SECONDS = 10.0

log = logging.getLogger(__name__)


class Heartbeat:
    """Pings `url` on success and `{url}/fail` on failure; does nothing when `url` is `None`.

    A ping that cannot be delivered is logged and swallowed: monitoring must never be the reason
    a run fails or the scheduler stops. The URL is never logged, since anyone holding it can ping
    the check.
    """

    def __init__(self, url: str | None, client: httpx.Client) -> None:
        self._url = url.rstrip("/") if url else None
        self._client = client

    def ok(self, body: str | None = None) -> None:
        """Ping success: a POST of `body` when given, which healthchecks.io shows beside the ping,
        else a bare GET, as nba-today's `ok()` sends."""
        self._ping("GET" if body is None else "POST", "", body)

    def fail(self, reason: str) -> None:
        """Ping `/fail` with `reason` as the body, which healthchecks.io shows beside the ping."""
        self._ping("POST", "/fail", reason)

    def _ping(self, method: str, suffix: str, body: str | None = None) -> None:
        if self._url is None:
            log.debug("heartbeat disabled", extra={"ping": suffix or "ok"})
            return
        try:
            response = self._client.request(
                method, self._url + suffix, content=body, timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
        # `InvalidURL` is not an `HTTPError`; a malformed `HEALTHCHECKS_URL` must not crash a run.
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # A status error's message quotes the URL, so log only its status code.
            error = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else str(exc)
            )
            log.warning("heartbeat ping failed", extra={"ping": suffix or "ok", "error": error})


def report_run(
    heartbeat: Heartbeat,
    *,
    run_id: str,
    status: FinalStatus,
    failed_sources: Sequence[str] = (),
    reason: str | None = None,
) -> None:
    """Ping how `run_id` ended: `/fail` with `reason` when it `failed`, success otherwise, each
    with a one-line body `status run_id failed_sources` (`-` when none failed)."""
    line = f"{status} {run_id} {','.join(failed_sources) or '-'}"
    if status == "failed":
        heartbeat.fail(f"{line}: {reason or 'no reason recorded'}")
    else:
        heartbeat.ok(line)


def failure_reason(exc: BaseException) -> str:
    """What `/fail` says about `exc`, the exception that stopped a run.

    A CLI step that fails echoes why and raises `typer.Exit` from the error behind it, so that
    error, when there is one, is the reason rather than the bare exit.
    """
    cause = exc.__cause__ or exc
    message = str(cause)
    return f"{type(cause).__name__}: {message}" if message else type(cause).__name__
