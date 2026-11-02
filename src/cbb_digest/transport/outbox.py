"""The dry-run transport: write the digest to the data volume instead of sending it (spec §3)."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from cbb_digest.transport.base import Attachment, Message, SendReceipt, TransportError

log = logging.getLogger(__name__)


class OutboxTransport:
    """Files `<directory>/<stem>.html` and `<stem>.txt`, and each attachment beside them as
    `<stem>-<filename>`.

    `stem` is the run id, so a second send of the same run replaces the first.
    """

    name = "outbox"

    def __init__(self, directory: Path, stem: str) -> None:
        self.directory = directory
        self._stem = stem

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        html_path = self.directory / f"{self._stem}.html"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            html_path.write_text(msg.html, encoding="utf-8")
            (self.directory / f"{self._stem}.txt").write_text(msg.text, encoding="utf-8")
            for attachment in attachments:
                # Only the name: an attachment's filename never chooses the directory.
                name = Path(attachment.filename).name
                (self.directory / f"{self._stem}-{name}").write_bytes(attachment.content)
        except OSError as exc:
            raise TransportError(f"outbox write to {self.directory} failed: {exc}") from exc
        log.info(
            "dry run: digest written to outbox",
            extra={
                "path": str(html_path),
                "subject": msg.subject,
                "to": msg.to,
                "attachments": [attachment.filename for attachment in attachments],
            },
        )
        return SendReceipt(
            transport=self.name, message_id=str(html_path), sent_at=datetime.now(UTC)
        )
