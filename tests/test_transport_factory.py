"""Settings → transport: the outbox under `DRY_RUN`, else Resend then SMTP (spec §3)."""

import logging
from pathlib import Path

import pytest

from cbb_digest.settings import Settings
from cbb_digest.transport.chain import ChainTransport
from cbb_digest.transport.factory import TransportConfigError, build_transport
from cbb_digest.transport.outbox import OutboxTransport
from cbb_digest.transport.resend import ResendTransport
from cbb_digest.transport.smtp import SmtpTransport

SMTP: dict[str, object] = {
    "smtp_username": "user",
    "smtp_password": "secret",
    "smtp_sender_address": "cbb@example.com",
}
RESEND: dict[str, object] = {
    "resend_api_key": "key",
    "resend_from": "CBB Digest <cbb@example.com>",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "cbbd_api_key": "test-key",
            "recipients": ["fan@example.com"],
            "data_dir": tmp_path,
            **overrides,
        }
    )


def test_dry_run_is_only_the_outbox_named_for_the_run(tmp_path: Path) -> None:
    transport = build_transport(
        _settings(tmp_path, dry_run=True, **RESEND, **SMTP), run_id="2027-W46"
    )

    assert isinstance(transport, OutboxTransport)
    assert transport.directory == tmp_path / "outbox"


def test_resend_then_smtp_when_both_are_configured(tmp_path: Path) -> None:
    transport = build_transport(_settings(tmp_path, **RESEND, **SMTP), run_id="2027-W46")

    assert isinstance(transport, ChainTransport)
    assert [type(t) for t in transport.transports] == [ResendTransport, SmtpTransport]


def test_resend_without_a_sender_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, resend_api_key="key", **SMTP)

    with caplog.at_level(logging.WARNING, logger="cbb_digest.transport.factory"):
        transport = build_transport(settings, run_id="2027-W46")

    assert isinstance(transport, ChainTransport)
    assert [t.name for t in transport.transports] == ["smtp"]
    [record] = caplog.records
    assert record.getMessage() == "transport not configured, skipping"
    assert vars(record)["transport"] == "resend"


@pytest.mark.parametrize("missing", ["smtp_username", "smtp_password", "smtp_sender_address"])
def test_incomplete_smtp_settings_skip_smtp(tmp_path: Path, missing: str) -> None:
    transport = build_transport(
        _settings(tmp_path, **RESEND, **{**SMTP, missing: None}), run_id="2027-W46"
    )

    assert isinstance(transport, ChainTransport)
    assert [t.name for t in transport.transports] == ["resend"]


def test_nothing_configured_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(TransportConfigError, match="no transport is configured"):
        build_transport(_settings(tmp_path), run_id="2027-W46")
