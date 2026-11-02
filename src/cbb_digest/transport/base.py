"""The transport contract: a rendered digest in, a receipt out (spec §3, §11)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Message:
    subject: str
    html: str
    text: str
    to: list[str]
    from_: str


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file sent with the message. Only the portal-reset digest attaches one (spec §11)."""

    filename: str
    content: bytes
    mime: str


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Which transport delivered the message, its id there (if it gives one), and when."""

    transport: str
    message_id: str | None
    sent_at: datetime


class TransportError(Exception):
    """A transport could not deliver a message; the next transport in the chain may."""


class AllTransportsFailed(TransportError):
    """Every transport in the chain failed; `errors` holds each one's name and error, in order."""

    def __init__(self, errors: list[tuple[str, Exception]]) -> None:
        detail = "; ".join(f"{name}: {exc}" for name, exc in errors)
        super().__init__(f"all transports failed ({detail})")
        self.errors = errors


class Transport(Protocol):
    name: str

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        """Deliver `msg` with `attachments`, or raise `TransportError`."""
        ...
