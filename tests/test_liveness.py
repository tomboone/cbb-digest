from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from cbb_digest import db, liveness
from cbb_digest.schema import heartbeat

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    yield engine
    engine.dispose()


def test_there_is_no_tick_before_the_first(engine: Engine) -> None:
    assert liveness.last_tick(engine) is None


def test_record_tick_keeps_a_single_row_with_the_latest_time(engine: Engine) -> None:
    liveness.record_tick(engine, NOW)
    liveness.record_tick(engine, NOW + timedelta(hours=1))

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(heartbeat)).scalar_one() == 1
    assert liveness.last_tick(engine) == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("ticked_at", "healthy"),
    [
        pytest.param(NOW, True, id="fresh"),
        pytest.param(NOW - timedelta(days=8) + timedelta(seconds=1), True, id="just-under-8-days"),
        pytest.param(NOW - timedelta(days=8), False, id="exactly-8-days"),
        pytest.param(NOW - timedelta(days=9), False, id="9-days"),
    ],
)
def test_is_healthy_while_the_tick_is_under_8_days_old(ticked_at: datetime, healthy: bool) -> None:
    assert liveness.is_healthy(ticked_at, NOW) is healthy
