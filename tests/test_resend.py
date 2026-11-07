"""The Resend transport against a respx stand-in for Resend's API."""

import base64
import json
from collections.abc import Iterator
from datetime import UTC

import httpx
import pytest
import respx

from cbb_digest.transport.base import Attachment, Message, TransportError
from cbb_digest.transport.resend import API_URL, ResendTransport

MESSAGE = Message(
    subject="CBB digest — week of Nov 9",
    html="<p>digest</p>",
    text="digest",
    to=["a@example.com", "b@example.com"],
    from_="CBB Digest <cbb@example.com>",
)


@pytest.fixture
def resend_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _transport() -> ResendTransport:
    return ResendTransport("re_test_key", "CBB Digest <cbb@tomboone.com>")


def _sent(router: respx.MockRouter) -> tuple[httpx.Request, dict[str, object]]:
    assert router.calls.call_count == 1
    request = router.calls.last.request
    return request, json.loads(request.content)


def test_sends_from_the_configured_sender_and_returns_the_resend_id(
    resend_api: respx.MockRouter,
) -> None:
    resend_api.post(API_URL).respond(200, json={"id": "49a3999c-0ce1"})

    receipt = _transport().send(MESSAGE)

    request, body = _sent(resend_api)
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert body == {
        "from": "CBB Digest <cbb@tomboone.com>",
        "to": ["a@example.com", "b@example.com"],
        "subject": "CBB digest — week of Nov 9",
        "html": "<p>digest</p>",
        "text": "digest",
    }
    assert receipt.transport == "resend"
    assert receipt.message_id == "49a3999c-0ce1"
    assert receipt.sent_at.tzinfo is UTC


def test_attachments_go_in_resends_attachments_field_as_base64(
    resend_api: respx.MockRouter,
) -> None:
    resend_api.post(API_URL).respond(200, json={"id": "abc"})
    content = b"season: 2028\nplayers: []\n"

    _transport().send(MESSAGE, [Attachment("watchlist-2028.yaml", content, "application/yaml")])

    _, body = _sent(resend_api)
    assert body["subject"] == MESSAGE.subject
    assert body["html"] == MESSAGE.html
    assert body["text"] == MESSAGE.text
    assert body["attachments"] == [
        {
            "filename": "watchlist-2028.yaml",
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": "application/yaml",
        }
    ]


def test_an_accepted_send_without_an_id_is_still_a_delivery(
    resend_api: respx.MockRouter,
) -> None:
    resend_api.post(API_URL).respond(200, json={})

    receipt = _transport().send(MESSAGE)

    assert receipt.transport == "resend"
    assert receipt.message_id is None


def test_an_api_error_is_a_transport_error_carrying_the_resend_message(
    resend_api: respx.MockRouter,
) -> None:
    resend_api.post(API_URL).respond(
        422,
        json={
            "statusCode": 422,
            "name": "validation_error",
            "message": "The tomboone.com domain is not verified.",
        },
    )

    with pytest.raises(TransportError, match=r"422: The tomboone\.com domain is not verified"):
        _transport().send(MESSAGE)


def test_a_server_error_without_json_is_a_transport_error(resend_api: respx.MockRouter) -> None:
    resend_api.post(API_URL).respond(500, text="upstream exploded")

    with pytest.raises(TransportError, match="HTTP 500: upstream exploded"):
        _transport().send(MESSAGE)


def test_a_network_failure_is_a_transport_error(resend_api: respx.MockRouter) -> None:
    resend_api.post(API_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(TransportError, match="connection refused"):
        _transport().send(MESSAGE)
