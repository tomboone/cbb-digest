from pathlib import Path

import pytest
from pydantic import ValidationError

from cbb_digest.settings import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CBBD_API_KEY", "key")
    monkeypatch.setenv("RECIPIENTS", "fan@example.com")


@pytest.mark.usefixtures("required")
def test_defaults() -> None:
    settings = Settings()

    assert settings.data_dir == Path("/data")
    assert settings.cbbd_api_key.get_secret_value() == "key"
    assert settings.recipients == ["fan@example.com"]
    assert settings.resend_api_key is None
    assert settings.resend_from is None
    assert settings.smtp_host == "smtp.fastmail.com"
    assert settings.smtp_port == 465
    assert settings.smtp_username is None
    assert settings.smtp_password is None
    assert settings.smtp_sender_address is None
    assert settings.healthchecks_url is None
    assert settings.dry_run is False
    assert settings.image_sha is None
    assert settings.log_level == "INFO"


@pytest.mark.usefixtures("required")
def test_recipients_parsed_from_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECIPIENTS", " a@example.com, b@example.com,,")

    assert Settings().recipients == ["a@example.com", "b@example.com"]


@pytest.mark.usefixtures("required")
def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert Settings().log_level == "DEBUG"


@pytest.mark.usefixtures("required")
def test_dry_run_and_image_sha_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("IMAGE_SHA", "abc1234")

    settings = Settings()

    assert settings.dry_run is True
    assert settings.image_sha == "abc1234"


def test_missing_cbbd_api_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECIPIENTS", "fan@example.com")

    with pytest.raises(ValidationError, match="cbbd_api_key"):
        Settings()


def test_blank_cbbd_api_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CBBD_API_KEY", "   ")
    monkeypatch.setenv("RECIPIENTS", "fan@example.com")

    with pytest.raises(ValidationError, match="CBBD_API_KEY is blank"):
        Settings()


def test_missing_recipients_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CBBD_API_KEY", "key")

    with pytest.raises(ValidationError, match="recipients"):
        Settings()


@pytest.mark.parametrize("value", ["", " , "])
def test_empty_recipients_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CBBD_API_KEY", "key")
    monkeypatch.setenv("RECIPIENTS", value)

    with pytest.raises(ValidationError, match="recipients"):
        Settings()
