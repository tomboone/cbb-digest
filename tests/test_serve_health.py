import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cbb_digest import cli, db, liveness
from cbb_digest.scheduler import Scheduler
from cbb_digest.settings import Settings
from tests.fakes import FakeClock

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CBBD_API_KEY", "key")
    monkeypatch.setenv("RECIPIENTS", "fan@example.com")
    return tmp_path


def _tick(data_dir: Path, at: datetime) -> None:
    engine = db.open_database(data_dir)
    liveness.record_tick(engine, at)
    engine.dispose()


def test_health_is_0_with_a_fresh_heartbeat(data_dir: Path) -> None:
    _tick(data_dir, datetime.now(UTC))

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_health_is_1_with_a_heartbeat_9_days_old(data_dir: Path) -> None:
    _tick(data_dir, datetime.now(UTC) - timedelta(days=9))

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "unhealthy" in result.output


def test_health_is_1_and_creates_nothing_without_a_database(data_dir: Path) -> None:
    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "unhealthy" in result.output
    assert not (data_dir / db.DATABASE_FILENAME).exists()


def test_health_is_1_before_serve_has_ticked(data_dir: Path) -> None:
    db.open_database(data_dir).dispose()

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "never ticked" in result.output


def test_health_is_1_on_an_unmigrated_database(data_dir: Path) -> None:
    sqlite3.connect(data_dir / db.DATABASE_FILENAME).close()

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 1
    assert "no such table: heartbeat" in result.output


def test_health_is_1_not_2_on_invalid_configuration(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Docker reserves health-check exit code 2.
    monkeypatch.delenv("CBBD_API_KEY")

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == 1


def test_serve_exits_2_on_invalid_configuration(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CBBD_API_KEY")

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 2


def test_serve_migrates_and_ticks_so_health_passes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One cycle on a fake clock, instead of looping forever on the real one.
    def fake_clock(stop: threading.Event | None = None) -> FakeClock:
        return FakeClock(datetime.now(UTC))

    def no_handler(signum: int, handler: object) -> None:
        return None

    monkeypatch.setattr(cli, "SystemClock", fake_clock)
    monkeypatch.setattr(Scheduler, "run_forever", Scheduler.cycle)
    monkeypatch.setattr(cli.signal, "signal", no_handler)

    serve = runner.invoke(cli.app, ["serve"])
    health = runner.invoke(cli.app, ["health"])

    assert serve.exit_code == 0, serve.output
    assert health.exit_code == 0, health.output
