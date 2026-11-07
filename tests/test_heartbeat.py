"""The healthchecks.io client, ported from nba-today, and how each run status pings it."""

import logging
from collections.abc import Iterator

import httpx
import pytest
import respx
import typer

from cbb_digest.heartbeat import (
    TIMEOUT_SECONDS,
    Heartbeat,
    failure_reason,
    report_run,
)
from cbb_digest.runs import FinalStatus

URL = "https://hc-ping.com/abc-123"


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


@respx.mock
def test_ok_gets_the_url(client: httpx.Client) -> None:
    route = respx.get(URL).respond(200, text="OK")

    Heartbeat(URL, client).ok()

    assert route.call_count == 1


@respx.mock
def test_ok_with_a_body_posts_it_to_the_url(client: httpx.Client) -> None:
    route = respx.post(URL).respond(200, text="OK")

    Heartbeat(URL, client).ok("sent 2027-W45 -")

    assert route.call_count == 1
    assert route.calls.last.request.content == b"sent 2027-W45 -"


@respx.mock
def test_fail_posts_the_reason_to_the_fail_url(client: httpx.Client) -> None:
    route = respx.post(f"{URL}/fail").respond(200, text="OK")

    Heartbeat(URL, client).fail("all transports failed")

    assert route.call_count == 1
    assert route.calls.last.request.content == b"all transports failed"


@respx.mock
def test_trailing_slash_on_the_url_is_ignored(client: httpx.Client) -> None:
    route = respx.post(f"{URL}/fail").respond(200, text="OK")

    Heartbeat(f"{URL}/", client).fail("all transports failed")

    assert route.call_count == 1


@respx.mock
def test_pings_use_the_heartbeat_timeout(client: httpx.Client) -> None:
    route = respx.get(URL).respond(200)

    Heartbeat(URL, client).ok()

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout == httpx.Timeout(TIMEOUT_SECONDS).as_dict()


@pytest.mark.respx(assert_all_called=False)
def test_unset_url_sends_nothing(
    client: httpx.Client, caplog: pytest.LogCaptureFixture, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.route()

    with caplog.at_level(logging.DEBUG, logger="cbb_digest.heartbeat"):
        heartbeat = Heartbeat(None, client)
        heartbeat.ok("sent 2027-W45 -")
        heartbeat.fail("all transports failed")

    assert route.call_count == 0
    assert caplog.messages == ["heartbeat disabled"] * 2


@pytest.mark.parametrize(
    "response",
    [httpx.Response(404, text="not found"), httpx.ConnectError("refused")],
    ids=["error-status", "connect-error"],
)
@respx.mock
def test_ping_failures_are_logged_not_raised(
    client: httpx.Client,
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response | Exception,
) -> None:
    respx.get(URL).mock(side_effect=[response])

    with caplog.at_level(logging.WARNING, logger="cbb_digest.heartbeat"):
        Heartbeat(URL, client).ok()

    assert caplog.messages == ["heartbeat ping failed"]
    # Anyone holding the URL can ping the check, so it never reaches the logs.
    assert not any(URL in str(record.__dict__) for record in caplog.records)


def test_malformed_url_is_logged_not_raised(
    client: httpx.Client, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="cbb_digest.heartbeat"):
        Heartbeat("https://hc-ping.com/abc\x00", client).fail("all transports failed")

    assert caplog.messages == ["heartbeat ping failed"]


# --- run status → ping ---


@pytest.mark.parametrize(
    ("status", "failed_sources", "path", "body"),
    [
        pytest.param("sent", [], "", "sent 2027-W45 -", id="sent"),
        pytest.param(
            "sent_degraded",
            ["cbbd", "espn"],
            "",
            "sent_degraded 2027-W45 cbbd,espn",
            id="sent_degraded",
        ),
        pytest.param(
            "skipped_offseason", [], "", "skipped_offseason 2027-W45 -", id="skipped_offseason"
        ),
        pytest.param("skipped_missed", [], "", "skipped_missed 2027-W45 -", id="skipped_missed"),
        pytest.param(
            "failed",
            ["torvik"],
            "/fail",
            "failed 2027-W45 torvik: TransportError: all transports failed",
            id="failed",
        ),
    ],
)
@respx.mock
def test_each_terminal_status_pings_its_url_with_a_one_line_body(
    client: httpx.Client, status: FinalStatus, failed_sources: list[str], path: str, body: str
) -> None:
    route = respx.post(f"{URL}{path}").respond(200, text="OK")

    report_run(
        Heartbeat(URL, client),
        run_id="2027-W45",
        status=status,
        failed_sources=failed_sources,
        reason="TransportError: all transports failed" if status == "failed" else None,
    )

    assert route.call_count == 1
    assert route.calls.last.request.content.decode() == body


class _BoomError(Exception):
    pass


def _exit_from(cause: Exception) -> typer.Exit:
    try:
        raise typer.Exit(1) from cause
    except typer.Exit as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        pytest.param(_BoomError("disk full"), "_BoomError: disk full", id="plain"),
        pytest.param(_BoomError(), "_BoomError", id="no-message"),
        pytest.param(
            _exit_from(_BoomError("no transport delivered")),
            "_BoomError: no transport delivered",
            id="exit-names-its-cause",
        ),
        pytest.param(typer.Exit(1), "Exit: 1", id="bare-exit"),
    ],
)
def test_failure_reason(exc: BaseException, reason: str) -> None:
    assert failure_reason(exc) == reason
