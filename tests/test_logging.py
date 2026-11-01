import json
import logging
from collections.abc import Iterator

import pytest

from cbb_digest.logging import configure_logging


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_emits_json_line_with_extras(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")

    logging.getLogger("cbb_digest.test").info("sent %s", "digest", extra={"transport": "resend"})

    record = json.loads(capsys.readouterr().out)
    assert record["level"] == "INFO"
    assert record["logger"] == "cbb_digest.test"
    assert record["msg"] == "sent digest"
    assert record["transport"] == "resend"
    assert record["ts"].endswith("+00:00")


def test_honours_level(root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")

    logging.getLogger("cbb_digest.test").info("quiet")

    assert capsys.readouterr().out == ""


def test_includes_exception(
    root_logger: logging.Logger, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("cbb_digest.test").exception("failed")

    record = json.loads(capsys.readouterr().out)
    assert "RuntimeError: boom" in record["exc"]
