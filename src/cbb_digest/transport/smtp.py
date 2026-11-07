"""SMTP over implicit TLS, the fallback transport (Fastmail in production)."""

import logging
import smtplib
import ssl
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid

from cbb_digest.transport.base import Attachment, Message, SendReceipt, TransportError

log = logging.getLogger(__name__)


class SmtpTransport:
    name = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._timeout = timeout

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        """Send a `multipart/alternative` email: the text part, then the HTML part. With
        attachments it becomes `multipart/mixed`: that alternative, then each attachment.

        The configured sender is both the From header and the envelope sender, not `msg.from_`:
        an SMTP account only relays mail from its own identities, and `msg.from_` is the same
        for every transport in the chain.
        """
        email = EmailMessage()
        email["Subject"] = msg.subject
        email["From"] = self._sender
        email["To"] = ", ".join(msg.to)
        email["Message-ID"] = make_msgid(domain=self._sender.rpartition("@")[2] or None)
        email.set_content(msg.text)
        email.add_alternative(msg.html, subtype="html")
        for attachment in attachments:
            maintype, _, subtype = attachment.mime.partition("/")
            email.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        try:
            with smtplib.SMTP_SSL(
                self._host,
                self._port,
                context=ssl.create_default_context(),
                timeout=self._timeout,
            ) as conn:
                conn.login(self._username, self._password)
                refused = conn.send_message(email, from_addr=self._sender, to_addrs=msg.to)
        except (smtplib.SMTPException, OSError) as exc:
            raise TransportError(f"SMTP send via {self._host} failed: {exc}") from exc
        if refused:
            # Some recipients got it, so this is a delivery, not a failure to retry elsewhere.
            log.warning("smtp recipients refused", extra={"refused": sorted(refused)})
        return SendReceipt(
            transport=self.name, message_id=email["Message-ID"], sent_at=datetime.now(UTC)
        )
