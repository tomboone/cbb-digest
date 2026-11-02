"""Settings → the transport `send` uses: the dry-run outbox, or Resend then SMTP (spec §3)."""

import logging
from collections.abc import Callable
from pathlib import Path

from cbb_digest.settings import Settings
from cbb_digest.transport.base import Transport
from cbb_digest.transport.chain import ChainTransport
from cbb_digest.transport.outbox import OutboxTransport
from cbb_digest.transport.resend import ResendTransport
from cbb_digest.transport.smtp import SmtpTransport

log = logging.getLogger(__name__)

OUTBOX_DIRNAME = "outbox"


class TransportConfigError(Exception):
    """Neither Resend nor SMTP is configured, so nothing could ever be sent."""


def _resend(settings: Settings) -> Transport | None:
    if settings.resend_api_key is None or settings.resend_from is None:
        return None
    return ResendTransport(settings.resend_api_key.get_secret_value(), settings.resend_from)


def _smtp(settings: Settings) -> Transport | None:
    if (
        settings.smtp_username is None
        or settings.smtp_password is None
        or settings.smtp_sender_address is None
    ):
        return None
    return SmtpTransport(
        settings.smtp_host,
        settings.smtp_port,
        settings.smtp_username,
        settings.smtp_password.get_secret_value(),
        settings.smtp_sender_address,
    )


# In chain order. Each builder returns None when its settings are incomplete.
_BUILDERS: dict[str, Callable[[Settings], Transport | None]] = {
    "resend": _resend,
    "smtp": _smtp,
}


def outbox_dir(data_dir: Path) -> Path:
    return data_dir / OUTBOX_DIRNAME


def build_transport(settings: Settings, *, run_id: str) -> Transport:
    """`DRY_RUN` → only the outbox under `DATA_DIR`, its files named for `run_id`; otherwise
    every configured transport, Resend first.

    A transport whose settings are incomplete is skipped with a warning. Raises
    `TransportConfigError` when that leaves the chain empty.
    """
    if settings.dry_run:
        return OutboxTransport(outbox_dir(settings.data_dir), run_id)
    transports: list[Transport] = []
    for name, builder in _BUILDERS.items():
        if (transport := builder(settings)) is None:
            log.warning("transport not configured, skipping", extra={"transport": name})
        else:
            transports.append(transport)
    if not transports:
        raise TransportConfigError(
            "no transport is configured: set RESEND_API_KEY and RESEND_FROM, or the SMTP_* "
            "credentials"
        )
    return ChainTransport(transports)
