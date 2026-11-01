from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from cbb_digest import db
from cbb_digest.schema import heartbeat, team_ratings_snapshot

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = db.open_database(tmp_path)
    yield engine
    engine.dispose()


def test_database_lives_in_the_data_dir(tmp_path: Path) -> None:
    assert db.database_url(tmp_path) == f"sqlite:///{tmp_path}/cbb-digest.sqlite3"


def test_open_database_creates_the_data_dir_and_upgrades_to_head(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    engine = db.open_database(data_dir)

    assert (data_dir / db.DATABASE_FILENAME).is_file()
    assert "heartbeat" in inspect(engine).get_table_names()
    engine.dispose()


def test_connections_enforce_foreign_keys_in_wal_mode(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"


def test_a_dangling_foreign_key_is_refused(engine: Engine) -> None:
    row = {
        "run_id": "2027-W46",
        "slug": "kansas",
        "rank": 1,
        "net": 30.0,
        "adj_o": 125.0,
        "adj_d": 95.0,
        "fetched_at": datetime(2026, 11, 9, 12, tzinfo=UTC),
    }

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(team_ratings_snapshot).values(row))


def test_ddl_rolls_back_with_its_transaction(engine: Engine) -> None:
    with pytest.raises(RuntimeError), engine.begin() as connection:
        connection.execute(text("CREATE TABLE scratch (id INTEGER)"))
        raise RuntimeError("fail mid-migration")

    assert "scratch" not in inspect(engine).get_table_names()


def test_datetimes_round_trip_as_aware_utc(engine: Engine) -> None:
    eastern = datetime(2026, 11, 9, 7, tzinfo=EASTERN)

    with engine.begin() as connection:
        connection.execute(insert(heartbeat).values(id=1, ticked_at=eastern))
        stored = connection.execute(select(heartbeat.c.ticked_at)).scalar_one()

    assert stored == eastern
    assert stored.tzinfo is UTC


def test_naive_datetimes_are_refused(engine: Engine) -> None:
    with pytest.raises(StatementError, match="naive datetime"), engine.begin() as connection:
        connection.execute(insert(heartbeat).values(id=1, ticked_at=datetime(2026, 11, 9, 7)))


def test_heartbeat_is_a_singleton(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(heartbeat).values(id=2, ticked_at=datetime.now(UTC)))
