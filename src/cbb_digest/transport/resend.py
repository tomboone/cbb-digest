"""Resend's HTTP API, the primary transport.

Called with httpx rather than the `resend` SDK, which sends through `requests`: httpx is already
the project's HTTP client, and respx can then stand in for Resend in the tests.
"""

import base64
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import httpx

from cbb_digest.transport.base import Attachment, Message, SendReceipt, TransportError

API_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 30.0


class ResendTransport:
    name = "resend"

    def __init__(self, api_key: str, sender: str, *, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._sender = sender
        self._client = client

    def send(self, msg: Message, attachments: Sequence[Attachment] = ()) -> SendReceipt:
        """Send `msg` through Resend, from the configured sender rather than `msg.from_`.

        Resend only sends from a verified domain, so the sender is configuration, as it is for
        SMTP. Attachments go in its `attachments` field, base64-encoded.
        """
        payload: dict[str, object] = {
            "from": self._sender,
            "to": msg.to,
            "subject": msg.subject,
            "html": msg.html,
            "text": msg.text,
        }
        if attachments:
            payload["attachments"] = [
                {
                    "filename": attachment.filename,
                    "content": base64.b64encode(attachment.content).decode("ascii"),
                    "content_type": attachment.mime,
                }
                for attachment in attachments
            ]
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(API_URL, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                    response = client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"Resend send failed: {exc}") from exc
        if not response.is_success:
            raise TransportError(
                f"Resend send failed: HTTP {response.status_code}: {_error_message(response)}"
            )
        # A 2xx means Resend accepted the mail, so a missing id still counts as a delivery.
        return SendReceipt(
            transport=self.name, message_id=_message_id(response), sent_at=datetime.now(UTC)
        )


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        body: object = response.json()
    except ValueError:
        return {}
    return cast(dict[str, object], body) if isinstance(body, dict) else {}


def _message_id(response: httpx.Response) -> str | None:
    message_id = _json_object(response).get("id")
    return message_id if isinstance(message_id, str) else None


def _error_message(response: httpx.Response) -> str:
    message = _json_object(response).get("message")
    return message if isinstance(message, str) else response.text[:200]
