"""`send` and `run`: the transport chain, the `sent_at` guard, and the `runs` row each run
leaves (spec §9.1, §9.2, §9.3).

The fixture week is `test_build`'s: season 2026, the week of 2026-11-02, in season through
ESPN's Tuesday. Resend is the respx route the source fixtures share; SMTP is `test_smtp`'s fake.
"""

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from respx.models import Call
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from cbb_digest import cli, db
from cbb_digest.cli import RETRY_DELAY, app
from cbb_digest.compute.build import BANNER
from cbb_digest.schema import runs
from cbb_digest.sources import cbbd, espn, torvik
from cbb_digest.transport import smtp
from cbb_digest.transport.resend import API_URL
from tests.fakes import FakeClock
from tests.test_build import MONDAY, RUN_ID
from tests.test_build import env as env
from tests.test_build import week as week
from tests.test_load import FIXTURES, scoreboard
from tests.test_load import sources as sources
from tests.test_smtp import FakeSMTP

WEEK = MONDAY.isoformat()
SUBJECT = "CBB digest — week of Nov 2"

runner = CliRunner()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """One fake clock behind every `SystemClock()` the CLI builds, so a retry's 30-minute wait
    returns at once and the cache sees the time it skipped."""
    fake = FakeClock(datetime.now(UTC))

    def system_clock(stop: threading.Event | None = None) -> FakeClock:
        return fake

    monkeypatch.setattr(cli, "SystemClock", system_clock)
    return fake


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSMTP]]:
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtp.smtplib, "SMTP_SSL", FakeSMTP)
    yield FakeSMTP


@pytest.fixture
def mail(env: Path, monkeypatch: pytest.MonkeyPatch, fake_smtp: type[FakeSMTP]) -> None:
    """Resend and the SMTP fallback both configured."""
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM", "CBB Digest <cbb@tomboone.com>")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_SENDER_ADDRESS", "cbb@example.com")


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _row(env: Path) -> dict[str, Any]:
    engine = db.open_database(env)
    try:
        with engine.connect() as connection:
            return dict(
                connection.execute(select(runs).where(runs.c.run_id == RUN_ID)).mappings().one()
            )
    finally:
        engine.dispose()


def _resend_ok(router: respx.MockRouter) -> respx.Route:
    return router.post(API_URL).respond(200, json={"id": "re_1"})


@pytest.fixture
def computed(week: Engine) -> None:
    """The fixture week computed, so out/digest.json is there to send."""
    code, output = _invoke("compute", "--week", WEEK)
    assert code == 0, output


# --- send ---


@pytest.mark.usefixtures("computed", "mail")
def test_send_delivers_through_resend_and_stamps_the_run(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)

    code, output = _invoke("send", "--week", WEEK)

    assert code == 0, output
    assert "sent via resend (re_1)" in output
    body = json.loads(route.calls.last.request.content)
    out = env / "out"
    assert body["subject"] == SUBJECT
    assert body["to"] == ["tom@example.com"]
    assert body["html"] == (out / "digest.html").read_text(encoding="utf-8")
    assert body["text"] == (out / "digest.txt").read_text(encoding="utf-8")
    assert "attachments" not in body
    assert FakeSMTP.instances == []
    row = _row(env)
    assert (row["transport"], row["status"]) == ("resend", "running")
    assert row["sent_at"] is not None


@pytest.mark.usefixtures("computed", "mail")
def test_a_resend_500_falls_back_to_smtp(sources: respx.MockRouter, env: Path) -> None:
    sources.post(API_URL).respond(500, json={"message": "internal error"})

    code, output = _invoke("send", "--week", WEEK)

    assert code == 0, output
    assert "sent via smtp" in output
    [conn] = FakeSMTP.instances
    [(email, _, to_addrs)] = conn.sent
    assert email["Subject"] == SUBJECT
    assert to_addrs == ["tom@example.com"]
    row = _row(env)
    assert row["transport"] == "smtp"
    assert row["sent_at"] is not None


@pytest.mark.usefixtures("computed", "mail")
def test_a_second_send_is_refused_without_force_and_sent_with_it(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)
    code, output = _invoke("send", "--week", WEEK)
    assert code == 0, output
    first_sent_at = _row(env)["sent_at"]

    code, output = _invoke("send", "--week", WEEK)

    assert code == 1
    assert f"run {RUN_ID} has already been sent; pass --force" in output
    assert route.call_count == 1
    assert _row(env)["sent_at"] == first_sent_at

    code, output = _invoke("send", "--week", WEEK, "--force")

    assert code == 0, output
    assert route.call_count == 2
    assert _row(env)["sent_at"] > first_sent_at


@pytest.mark.usefixtures("computed", "mail")
def test_every_transport_failing_exits_1_and_leaves_the_run_unsent(
    sources: respx.MockRouter, env: Path
) -> None:
    sources.post(API_URL).respond(500)
    FakeSMTP.fail_with = ConnectionRefusedError("refused")

    code, output = _invoke("send", "--week", WEEK)

    assert code == 1
    assert "send failed: all transports failed" in output
    assert _row(env)["sent_at"] is None


@pytest.mark.usefixtures("computed")
def test_send_with_no_transport_configured_exits_2(env: Path) -> None:
    code, output = _invoke("send", "--week", WEEK)

    assert code == 2
    assert "no transport is configured" in output


@pytest.mark.usefixtures("computed", "mail")
def test_send_for_a_run_with_no_row_exits_1_before_sending(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)
    (env / db.DATABASE_FILENAME).unlink()

    code, output = _invoke("send", "--week", WEEK)

    assert code == 1
    assert f"no run {RUN_ID}" in output
    assert route.call_count == 0


@pytest.mark.usefixtures("env")
def test_send_without_a_computed_digest_exits_1() -> None:
    code, output = _invoke("send", "--week", WEEK)

    assert code == 1
    assert "run `cbb-digest compute` first" in output


# --- run ---


@pytest.mark.usefixtures("sources")
def test_run_dry_run_over_the_fixtures_writes_out_and_records_a_sent_run(env: Path) -> None:
    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert "status: sent" in output
    out = env / "out"
    for name in ("digest.json", "digest.html", "digest.txt"):
        assert (out / name).is_file(), name
    outbox = env / "outbox"
    assert (outbox / f"{RUN_ID}.html").read_text(encoding="utf-8") == (
        (out / "digest.html").read_text(encoding="utf-8")
    )
    row = _row(env)
    assert row["status"] == "sent"
    assert row["transport"] == "outbox"
    assert row["sent_at"] is not None
    assert row["finished_at"] is not None
    assert row["failed_sources"] == []
    assert row["row_counts"]["espn week"] == 29
    assert [alert.split(" (")[0] for alert in row["alerts"]] == [
        "team_changed: Otega Oweh",
        "unresolved: Nobody Real",
    ]


@pytest.mark.usefixtures("mail")
def test_a_dry_run_does_not_block_the_real_send_of_the_same_week(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)
    code, output = _invoke("run", "--week", WEEK, "--dry-run")
    assert code == 0, output

    code, output = _invoke("run", "--week", WEEK)

    assert code == 0, output
    assert route.call_count == 1
    row = _row(env)
    assert (row["status"], row["transport"]) == ("sent", "resend")


@pytest.mark.usefixtures("mail")
def test_a_sent_week_refuses_another_run_without_force(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)
    code, output = _invoke("run", "--week", WEEK)
    assert code == 0, output
    sent = _row(env)

    code, output = _invoke("run", "--week", WEEK)

    assert code == 1
    assert "has already been sent; pass --force" in output
    assert _row(env) == sent

    code, output = _invoke("run", "--week", WEEK, "--force")

    assert code == 0, output
    assert route.call_count == 2


@pytest.mark.usefixtures("mail")
def test_a_dry_run_of_a_sent_week_keeps_the_record_of_the_real_send(
    sources: respx.MockRouter, env: Path
) -> None:
    route = _resend_ok(sources)
    code, output = _invoke("run", "--week", WEEK)
    assert code == 0, output
    sent = _row(env)

    for extra in ((), ("--force",)):
        args = ("run", "--week", WEEK, "--dry-run", *extra)
        code, output = _invoke(*args)

        assert code == 0, (args, output)
        row = _row(env)
        assert (row["sent_at"], row["transport"]) == (sent["sent_at"], "resend")
    assert (env / "outbox" / f"{RUN_ID}.html").is_file()

    code, output = _invoke("run", "--week", WEEK)

    assert code == 1
    assert "has already been sent" in output
    assert route.call_count == 1


@pytest.mark.usefixtures("mail")
def test_a_failed_source_sends_degraded(sources: respx.MockRouter, env: Path) -> None:
    _resend_ok(sources)
    sources.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}").respond(503)

    code, output = _invoke("run", "--week", WEEK)

    assert code == 0, output
    row = _row(env)
    assert (row["status"], row["transport"]) == ("sent_degraded", "resend")
    assert row["failed_sources"] == ["cbbd"]
    assert "cbbd ratings" not in row["row_counts"]


def _down_until(
    clock: FakeClock, until: datetime, up: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """A respx side effect: HTTP 503 before `until` on `clock`, then whatever `up` answers."""

    def answer(request: httpx.Request) -> httpx.Response:
        if clock.now() < until:
            return httpx.Response(503)
        return up(request)

    return answer


def _routes(router: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "player season": router.get(f"{torvik.BASE_URL}{torvik.PLAYER_SEASON_PATH}"),
        "game logs": router.get(f"{torvik.BASE_URL}{torvik.player_games_path(2026)}"),
        "ratings": router.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}"),
        # Fetched too: the run starts with `teams` empty.
        "teams": router.get(f"{cbbd.BASE_URL}{cbbd.TEAMS_PATH}"),
    }


def _digest(env: Path) -> dict[str, Any]:
    return json.loads((env / "out" / "digest.json").read_text(encoding="utf-8"))


def test_a_source_that_recovers_within_30_minutes_is_refetched_and_the_run_sends_complete(
    sources: respx.MockRouter, env: Path, clock: FakeClock
) -> None:
    retry_at = clock.now() + RETRY_DELAY
    sources.get(f"{espn.BASE_URL}{espn.SCOREBOARD_PATH}").mock(
        side_effect=_down_until(clock, retry_at, scoreboard)
    )

    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert "status: sent" in output
    assert clock.sleeps == [retry_at]
    row = _row(env)
    assert row["status"] == "sent"
    assert row["failed_sources"] == []
    assert row["row_counts"]["espn week"] == 29
    digest = _digest(env)
    assert digest["banner"] == []
    assert digest["featured"]["rows"]
    # The sources that answered the first time are never asked again.
    for name, route in _routes(sources).items():
        assert route.call_count == 1, name


def test_a_source_still_failing_after_the_retry_sends_degraded_without_its_sections(
    sources: respx.MockRouter, env: Path, clock: FakeClock
) -> None:
    # Opening week is in season only by ESPN's games this week (§8.2), so with ESPN down the
    # game logs need a game last week for the week to send at all.
    logs = json.loads((FIXTURES / "torvik/all_advgames-y2026-2026-11-01.json").read_bytes())
    logs[0][0] = "20261031"
    sources.get(f"{torvik.BASE_URL}{torvik.player_games_path(2026)}").respond(200, json=logs)
    retry_at = clock.now() + RETRY_DELAY
    sources.get(f"{espn.BASE_URL}{espn.SCOREBOARD_PATH}").respond(503)

    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert "status: sent_degraded" in output
    assert clock.sleeps == [retry_at]
    row = _row(env)
    assert row["status"] == "sent_degraded"
    assert row["failed_sources"] == ["espn"]
    assert "espn week" not in row["row_counts"]
    digest = _digest(env)
    assert digest["banner"] == [BANNER["espn"]]
    assert digest["featured"] is None
    for section in ("watchlist", "freshmen", "movers"):
        assert digest[section] is not None, section
    for name, route in _routes(sources).items():
        assert route.call_count == 1, name


def test_a_challenge_page_is_retried_once_then_degrades(
    sources: respx.MockRouter, env: Path, clock: FakeClock
) -> None:
    # Torvik's browser check answers 200 text/html (§6): a failure, refetched once, never solved.
    route = sources.get(f"{torvik.BASE_URL}{torvik.PLAYER_SEASON_PATH}").respond(
        200,
        content=(FIXTURES / "torvik/playerstat-challenge-2026-11-01.html").read_bytes(),
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )

    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert len(clock.sleeps) == 1
    assert route.call_count == 2
    row = _row(env)
    assert (row["status"], row["failed_sources"]) == ("sent_degraded", ["torvik"])


def test_no_retry_degrades_at_once(sources: respx.MockRouter, env: Path, clock: FakeClock) -> None:
    route = sources.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}").respond(404)

    code, output = _invoke("run", "--week", WEEK, "--dry-run", "--no-retry")

    assert code == 0, output
    assert clock.sleeps == []
    assert route.call_count == 1
    row = _row(env)
    assert (row["status"], row["failed_sources"]) == ("sent_degraded", ["cbbd"])


def test_a_run_whose_sources_all_answer_does_not_wait(
    sources: respx.MockRouter, env: Path, clock: FakeClock
) -> None:
    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert clock.sleeps == []


def test_ratings_cbbd_has_not_published_yet_leave_every_team_unranked(
    sources: respx.MockRouter, env: Path
) -> None:
    # CBBD answers `[]` until the season's first games (§6): a valid week, not a failed source.
    sources.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}").respond(200, json=[])

    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert "status: sent" in output
    row = _row(env)
    assert row["status"] == "sent"
    assert row["failed_sources"] == []
    assert row["row_counts"]["cbbd ratings"] == 0
    digest = json.loads((env / "out" / "digest.json").read_text(encoding="utf-8"))
    assert digest["banner"] == []
    assert digest["freshmen"]["rows"] == []
    assert digest["movers"]["baseline"] is True
    games = digest["featured"]["rows"]
    assert games
    assert all(game[side]["rank"] is None for game in games for side in ("home", "away"))
    text = (env / "out" / "digest.txt").read_text(encoding="utf-8")
    assert "No freshmen qualify yet." in text
    assert "Baseline snapshot taken — movers start next week." in text


@pytest.mark.usefixtures("mail")
def test_a_send_that_fails_everywhere_records_a_failed_run(
    sources: respx.MockRouter, env: Path
) -> None:
    sources.post(API_URL).respond(500)
    FakeSMTP.fail_with = ConnectionRefusedError("refused")

    code, output = _invoke("run", "--week", WEEK)

    assert code == 1
    assert "send failed" in output
    row = _row(env)
    assert row["status"] == "failed"
    assert row["sent_at"] is None
    assert row["finished_at"] is not None


@pytest.mark.usefixtures("sources")
def test_an_out_of_season_week_is_skipped_and_sends_nothing(env: Path) -> None:
    code, output = _invoke("run", "--week", "2026-07-06", "--dry-run")

    assert code == 0, output
    assert "status: skipped_offseason" in output
    engine = db.open_database(env)
    try:
        with engine.connect() as connection:
            row = connection.execute(select(runs).where(runs.c.run_id == "2026-W28")).one()
    finally:
        engine.dispose()
    assert row.status == "skipped_offseason"
    assert row.sent_at is None
    assert not (env / "outbox").exists()


@pytest.mark.usefixtures("env")
def test_run_with_no_transport_configured_exits_2_before_fetching(
    sources: respx.MockRouter,
) -> None:
    code, output = _invoke("run", "--week", WEEK)

    assert code == 2
    assert "no transport is configured" in output
    assert not sources.calls


# --- healthchecks ---

HC_URL = "https://hc-ping.com/abc-123"


@pytest.fixture
def pings(sources: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> dict[str, respx.Route]:
    """`HEALTHCHECKS_URL` set, and its success and `/fail` routes on the shared router."""
    monkeypatch.setenv("HEALTHCHECKS_URL", HC_URL)
    return {
        "ok": sources.route(method__in=["GET", "POST"], url=HC_URL).respond(200, text="OK"),
        "fail": sources.post(f"{HC_URL}/fail").respond(200, text="OK"),
    }


def _pinged(route: respx.Route) -> list[str]:
    # `CallList` is an untyped `list`; its elements are `Call`s.
    calls: list[Call] = list(route.calls)
    return [call.request.content.decode() for call in calls]


@pytest.mark.usefixtures("mail")
def test_a_sent_run_pings_success_with_its_status(
    sources: respx.MockRouter, pings: dict[str, respx.Route]
) -> None:
    _resend_ok(sources)

    code, output = _invoke("run", "--week", WEEK)

    assert code == 0, output
    assert _pinged(pings["ok"]) == [f"sent {RUN_ID} -"]
    assert not pings["fail"].called


@pytest.mark.usefixtures("mail")
def test_a_degraded_run_pings_success_naming_the_failed_sources(
    sources: respx.MockRouter, pings: dict[str, respx.Route]
) -> None:
    _resend_ok(sources)
    sources.get(f"{cbbd.BASE_URL}{cbbd.RATINGS_PATH}").respond(503)

    code, output = _invoke("run", "--week", WEEK, "--no-retry")

    assert code == 0, output
    assert _pinged(pings["ok"]) == [f"sent_degraded {RUN_ID} cbbd"]


@pytest.mark.usefixtures("env")
def test_an_out_of_season_run_pings_success(
    monkeypatch: pytest.MonkeyPatch, pings: dict[str, respx.Route]
) -> None:
    # `DRY_RUN=true` is how `serve` runs a week before going live: still a Monday to ping.
    monkeypatch.setenv("DRY_RUN", "true")

    code, output = _invoke("run", "--week", "2026-07-06")

    assert code == 0, output
    assert _pinged(pings["ok"]) == ["skipped_offseason 2026-W28 -"]


@pytest.mark.usefixtures("mail")
def test_a_failed_run_pings_fail_with_the_reason(
    sources: respx.MockRouter, pings: dict[str, respx.Route]
) -> None:
    sources.post(API_URL).respond(500)
    FakeSMTP.fail_with = ConnectionRefusedError("refused")

    code, output = _invoke("run", "--week", WEEK)

    assert code == 1, output
    assert not pings["ok"].called
    [body] = _pinged(pings["fail"])
    assert body.startswith(f"failed {RUN_ID} -: ")
    assert "AllTransportsFailed: all transports failed" in body


@pytest.mark.usefixtures("env")
def test_run_dry_run_never_pings(pings: dict[str, respx.Route]) -> None:
    code, output = _invoke("run", "--week", WEEK, "--dry-run")

    assert code == 0, output
    assert not pings["ok"].called
    assert not pings["fail"].called


@pytest.mark.usefixtures("mail")
def test_an_unset_healthchecks_url_makes_no_request(sources: respx.MockRouter) -> None:
    _resend_ok(sources)
    healthchecks = sources.route(host="hc-ping.com")

    code, output = _invoke("run", "--week", WEEK)

    assert code == 0, output
    assert not healthchecks.called
