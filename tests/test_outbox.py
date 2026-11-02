"""The dry-run outbox: files under the data volume instead of an email (spec §3, §11)."""

import logging
from datetime import UTC
from pathlib import Path

import pytest

from cbb_digest.runs import DRY_RUN_TRANSPORT
from cbb_digest.transport.base import Attachment, Message, TransportError
from cbb_digest.transport.outbox import OutboxTransport

MESSAGE = Message(
    subject="CBB digest — week of Nov 9",
    html="<p>digest</p>",
    text="digest",
    to=["fan@example.com"],
    from_="cbb@example.com",
)


def test_writes_html_and_text_named_for_the_run(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"

    receipt = OutboxTransport(outbox, "2027-W46").send(MESSAGE)

    assert (outbox / "2027-W46.html").read_text(encoding="utf-8") == "<p>digest</p>"
    assert (outbox / "2027-W46.txt").read_text(encoding="utf-8") == "digest"
    assert receipt.transport == "outbox"
    assert receipt.message_id == str(outbox / "2027-W46.html")
    assert receipt.sent_at.tzinfo is UTC


def test_is_the_transport_the_send_guard_treats_as_a_dry_run() -> None:
    assert OutboxTransport.name == DRY_RUN_TRANSPORT


def test_writes_attachments_beside_the_html_under_their_own_name_only(tmp_path: Path) -> None:
    attachments = [
        Attachment("watchlist-2028.yaml", b"season: 2028\n", "application/yaml"),
        Attachment("../escape.txt", b"x", "text/plain"),
    ]

    OutboxTransport(tmp_path, "reset-2028-2027-04-28").send(MESSAGE, attachments)

    assert (tmp_path / "reset-2028-2027-04-28-watchlist-2028.yaml").read_bytes() == (
        b"season: 2028\n"
    )
    assert (tmp_path / "reset-2028-2027-04-28-escape.txt").read_bytes() == b"x"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_logs_where_it_wrote(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cbb_digest.transport.outbox"):
        receipt = OutboxTransport(tmp_path, "2027-W46").send(MESSAGE)

    [record] = caplog.records
    assert vars(record)["path"] == receipt.message_id
    assert vars(record)["subject"] == MESSAGE.subject


def test_unwritable_directory_is_a_transport_error(tmp_path: Path) -> None:
    blocker = tmp_path / "outbox"
    blocker.write_text("not a directory")

    with pytest.raises(TransportError, match="outbox"):
        OutboxTransport(blocker, "2027-W46").send(MESSAGE)
