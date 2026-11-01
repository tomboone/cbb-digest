"""All runtime configuration, read from the environment (spec §9.5).

The season is not here: it comes from `watchlist.yaml`.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    data_dir: Path = Path("/data")

    # Required; `default=...` only tells pyright that env, not __init__, supplies it.
    cbbd_api_key: SecretStr = Field(default=...)
    recipients: Annotated[list[str], NoDecode] = Field(default=..., min_length=1)

    resend_api_key: SecretStr | None = None
    resend_from: str | None = None

    smtp_host: str = "smtp.fastmail.com"
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender_address: str | None = None

    healthchecks_url: str | None = None
    dry_run: bool = False
    # Baked into the image at build time; unset when running from a checkout.
    image_sha: str | None = None
    log_level: LogLevel = "INFO"

    @field_validator("recipients", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("cbbd_api_key")
    @classmethod
    def _reject_blank_key(cls, value: SecretStr) -> SecretStr:
        # An unset or empty variable is already "missing"; this catches one that is only spaces,
        # which would otherwise surface on the first run as a CBBD 401 source failure.
        if not value.get_secret_value().strip():
            raise ValueError("CBBD_API_KEY is blank")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
