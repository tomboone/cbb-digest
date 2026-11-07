"""The transport chain: first delivery wins, failures fall through in order (spec §3)."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from cbb_digest.transport.base import (
    AllTransportsFailed,
    Attachment,
    Message,
    SendReceipt,
    TransportError,
)
from cbb_digest.transport.chain import ChainTransport

MESSAGE = Message(
    subject="CBB digest — week of Nov 9",
    html="<p>digest</p>",
    text="digest",
    to=["fan@example.com"],
    from_="cbb@example.com",
)
ATTACHMENT = Attachment("watchlist-2028.yaml", b"season: 2028\n", "application/yaml")


class Failing:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        self.calls += 1
        raise TransportError(f"{self.name} is down")


class Succeeding:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.attachments: list[Sequence[Attachment]] = []

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        self.calls += 1
        self.attachments.append(attachments)
        return SendReceipt(
            transport=self.name, message_id=f"<{self.name}>", sent_at=datetime.now(UTC)
        )


def test_first_success_wins_without_trying_the_rest() -> None:
    first, second = Succeeding("a"), Succeeding("b")

    receipt = ChainTransport([first, second]).send(MESSAGE)

    assert receipt.transport == "a"
    assert (first.calls, second.calls) == (1, 0)


def test_falls_back_to_the_next_transport_and_logs_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, succeeding = Failing("resend"), Succeeding("smtp")

    with caplog.at_level(logging.WARNING, logger="cbb_digest.transport.chain"):
        receipt = ChainTransport([failing, succeeding]).send(MESSAGE)

    assert receipt.transport == "smtp"
    assert receipt.message_id == "<smtp>"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert vars(warnings[0])["transport"] == "resend"


def test_attachments_reach_the_transport_that_delivers() -> None:
    succeeding = Succeeding("smtp")

    ChainTransport([Failing("resend"), succeeding]).send(MESSAGE, [ATTACHMENT])

    assert succeeding.attachments == [[ATTACHMENT]]


def test_all_failing_raises_with_every_error_in_order() -> None:
    with pytest.raises(AllTransportsFailed) as raised:
        ChainTransport([Failing("resend"), Failing("smtp")]).send(MESSAGE)

    errors = raised.value.errors
    assert [name for name, _ in errors] == ["resend", "smtp"]
    assert [str(exc) for _, exc in errors] == ["resend is down", "smtp is down"]


def test_non_transport_errors_propagate() -> None:
    class Broken:
        name = "broken"

        def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
            raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        ChainTransport([Broken(), Succeeding("smtp")]).send(MESSAGE)


def test_empty_chain_rejected() -> None:
    with pytest.raises(ValueError, match="at least one transport"):
        ChainTransport([])
