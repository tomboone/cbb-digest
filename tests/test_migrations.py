from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from cbb_digest import db
from cbb_digest.schema import metadata

# Spec §9.4: every table and its key.
PRIMARY_KEYS = {
    "teams": ["slug"],
    "team_ratings_snapshot": ["run_id", "slug"],
    "players": ["season", "torvik_pid"],
    "player_games": ["season", "torvik_pid", "game_date", "opponent_slug"],
    "games": ["espn_id"],
    "watchlist_resolution": ["season", "watchlist_key"],
    "runs": ["run_id"],
    "heartbeat": ["id"],
}


@pytest.fixture
def url(tmp_path: Path) -> str:
    return db.database_url(tmp_path)


def _schema_sql(url: str) -> list[tuple[str, str, str | None]]:
    engine = db.create_engine(url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        )
        result = [(row.type, row.name, row.sql) for row in rows]
    engine.dispose()
    return result


def _version(url: str) -> list[str]:
    engine = db.create_engine(url)
    with engine.connect() as connection:
        result = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    engine.dispose()
    return result


def test_upgrade_creates_every_table_and_key(url: str) -> None:
    db.upgrade(url)

    engine = db.create_engine(url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {*PRIMARY_KEYS, "alembic_version"}
    for table, key in PRIMARY_KEYS.items():
        assert inspector.get_pk_constraint(table)["constrained_columns"] == key, table
    engine.dispose()


def test_head_matches_schema_module(url: str) -> None:
    db.upgrade(url)

    engine = db.create_engine(url)
    with engine.connect() as connection:
        diffs = compare_metadata(MigrationContext.configure(connection), metadata)
    engine.dispose()
    assert diffs == []


def test_downgrade_to_base_drops_every_table(url: str) -> None:
    db.upgrade(url)

    db.downgrade(url, "base")

    engine = db.create_engine(url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
    assert _version(url) == []


def test_upgrade_twice_is_a_no_op(url: str) -> None:
    db.upgrade(url)
    schema, version = _schema_sql(url), _version(url)

    db.upgrade(url)

    assert _schema_sql(url) == schema
    assert _version(url) == version == ["0001"]


def test_baseline_reruns_over_existing_tables(url: str) -> None:
    # Spec §9.4: every migration is idempotent, so a database whose version row was lost (or
    # whose tables were created by a half-run migration) still upgrades cleanly.
    db.upgrade(url)
    schema = _schema_sql(url)
    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
    engine.dispose()

    db.upgrade(url)

    assert _schema_sql(url) == schema
    assert _version(url) == ["0001"]


def test_migrations_contain_no_alembic_tmp_statements() -> None:
    versions = sorted((db.MIGRATIONS_DIR / "versions").glob("*.py"))

    assert versions
    for path in versions:
        assert "_alembic_tmp_" not in path.read_text(), path.name


def test_a_migration_that_leaves_dangling_foreign_keys_rolls_back(url: str) -> None:
    # Migrations run with foreign keys off (batch rebuilds need it), so they are checked at the end.
    db.upgrade(url)
    db.downgrade(url, "base")
    engine = db.create_engine(url, foreign_keys=False)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("CREATE TABLE teams (slug VARCHAR PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE players (season INTEGER, torvik_pid INTEGER, name VARCHAR,"
                " normalized_name VARCHAR, team_slug VARCHAR REFERENCES teams (slug),"
                " class_year VARCHAR, games INTEGER, min FLOAT, pts INTEGER, reb INTEGER,"
                " ast INTEGER, fga INTEGER, fta INTEGER, PRIMARY KEY (season, torvik_pid))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO players VALUES"
                " (2027, 1, 'A', 'a', 'nowhere', 'Fr', 0, 0, 0, 0, 0, 0, 0)"
            )
        )
    engine.dispose()

    with pytest.raises(db.DanglingForeignKeyError, match="players"):
        db.upgrade(url)

    engine = db.create_engine(url)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()
