"""`send` and `run`: the transport chain, the `sent_at` guard, and the `runs` row each run
leaves (spec §9.1, §9.2, §9.3).

The fixture week is `test_build`'s: season 2026, the week of 2026-11-02, in season through
ESPN's Tuesday. Resend is the respx route the source fixtures share; SMTP is `test_smtp`'s fake.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from cbb_digest import db
from cbb_digest.cli import app
from cbb_digest.schema import runs
from cbb_digest.sources import cbbd
from cbb_digest.transport import smtp
from cbb_digest.transport.resend import API_URL
from tests.test_build import MONDAY, RUN_ID
from tests.test_build import env as env
from tests.test_build import week as week
from tests.test_load import sources as sources
from tests.test_smtp import FakeSMTP

WEEK = MONDAY.isoformat()
SUBJECT = "CBB digest — week of Nov 2"

runner = CliRunner()


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
