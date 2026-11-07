"""The SQLite engine, and bringing a database up to the latest migration (spec §9.4, ADR-0001).

Every command that touches the database opens it through `open_database`, which runs
`alembic upgrade head` first, so a fresh volume or a newly deployed image never runs against an
old schema.
"""

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sa_create_engine

DATABASE_FILENAME = "cbb-digest.sqlite3"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class DanglingForeignKeyError(RuntimeError):
    """A migration left rows whose foreign keys point nowhere; it has been rolled back."""

    def __init__(self, rows: Sequence[Any]) -> None:
        # Each row is (table, rowid, referenced table, foreign key index).
        super().__init__(f"migration left dangling foreign keys: {[tuple(r) for r in rows]}")


def database_url(data_dir: Path) -> str:
    return f"sqlite:///{data_dir / DATABASE_FILENAME}"


def create_engine(url: str, *, foreign_keys: bool = True) -> Engine:
    """A SQLAlchemy core engine with WAL and, unless told otherwise, foreign keys enforced.

    The driver runs in Python 3.12+'s `autocommit=False` mode, so DDL is transactional: a
    migration that fails partway rolls back instead of leaving half its tables behind.
    """
    engine = sa_create_engine(url)

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: Any, _record: Any) -> None:  # pyright: ignore[reportUnusedFunction]
        connection = cast(sqlite3.Connection, dbapi_connection)
        # Both pragmas are ignored, or refused, inside a transaction, so set them in autocommit
        # mode before switching to the transactional one.
        connection.autocommit = True
        connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.autocommit = False

    return engine


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """Run `alembic upgrade` against the database at `url`, in a single transaction.

    Foreign keys are off for the migration: a batch-mode table rebuild drops and recreates the
    table, which enforcement would refuse (or cascade) while other tables reference it. So the
    references are checked once at the end instead, and any dangling one rolls the migration back.
    """
    _run_alembic(url, lambda config: command.upgrade(config, revision))


def downgrade(url: str, revision: str) -> None:
    _run_alembic(url, lambda config: command.downgrade(config, revision))


def _run_alembic(url: str, run: Callable[[Config], None]) -> None:
    engine = create_engine(url, foreign_keys=False)
    try:
        with engine.begin() as connection:
            config = alembic_config()
            config.attributes["connection"] = connection
            run(config)
            dangling = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if dangling:
                raise DanglingForeignKeyError(dangling)
    finally:
        engine.dispose()


def open_database(data_dir: Path) -> Engine:
    """Upgrade the database in `data_dir` to head, creating it if needed, and return an engine."""
    data_dir.mkdir(parents=True, exist_ok=True)
    url = database_url(data_dir)
    upgrade(url)
    return create_engine(url)
