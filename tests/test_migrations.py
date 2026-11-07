from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

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


def _unordered(
    schema: list[tuple[str, str, str | None]],
) -> list[tuple[str, str, list[str] | None]]:
    """`schema` with each definition's lines sorted, so a reordered constraint compares equal."""
    return [
        (kind, name, sorted(line.strip().rstrip(",") for line in sql.splitlines()) if sql else None)
        for kind, name, sql in schema
    ]


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
    assert _version(url) == version == ["0004"]


def test_baseline_reruns_over_existing_tables(url: str) -> None:
    # Spec §9.4: every migration is idempotent, so a database whose version row was lost (or
    # whose tables were created by a half-run migration) still upgrades cleanly.
    db.upgrade(url)
    schema = _unordered(_schema_sql(url))
    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
    engine.dispose()

    db.upgrade(url)

    # A batch rebuild (0002) may reorder a table's constraints, but not change them.
    assert _unordered(_schema_sql(url)) == schema
    assert _version(url) == ["0004"]


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


def test_unmapped_team_alerts_need_0002_and_its_downgrade_drops_them(url: str) -> None:
    db.upgrade(url)
    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs (run_id, kind, season, status, started_at)"
                " VALUES ('2027-W45', 'digest', 2027, 'running', '2026-11-02 12:00:00')"
            )
        )
        for key, kind in (("a|?x", "unmapped_team"), ("b|—", "unresolved")):
            connection.execute(
                text(
                    "INSERT INTO watchlist_resolution (season, watchlist_key, outcome, alert_kind,"
                    " first_run_id, changed_run_id)"
                    " VALUES (2027, :key, 'unresolved', :kind, '2027-W45', '2027-W45')"
                ),
                {"key": key, "kind": kind},
            )
    engine.dispose()

    db.downgrade(url, "0001")

    engine = db.create_engine(url)
    with engine.connect() as connection:
        keys = connection.execute(text("SELECT watchlist_key FROM watchlist_resolution"))
        assert keys.scalars().all() == ["b|—"]
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE watchlist_resolution SET alert_kind = 'unmapped_team'"
                " WHERE watchlist_key = 'b|—'"
            )
        )
    engine.dispose()


def test_0002_reruns_to_the_same_table(url: str) -> None:
    db.upgrade(url)
    before = _unordered(_schema_sql(url))
    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    engine.dispose()

    db.upgrade(url)

    assert _unordered(_schema_sql(url)) == before
    assert _version(url) == ["0004"]


def _game_columns(url: str) -> list[str]:
    engine = db.create_engine(url)
    columns = [column["name"] for column in inspect(engine).get_columns("games")]
    engine.dispose()
    return columns


def test_0003_adds_the_side_names_reruns_and_downgrades(url: str) -> None:
    db.upgrade(url, "0002")
    assert "home_name" not in _game_columns(url)

    db.upgrade(url)
    after = _schema_sql(url)
    assert {"home_name", "away_name"} <= set(_game_columns(url))

    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0002'"))
    engine.dispose()
    db.upgrade(url)
    assert _schema_sql(url) == after
    assert _version(url) == ["0004"]

    db.downgrade(url, "0002")
    assert not {"home_name", "away_name"} & set(_game_columns(url))
    assert _version(url) == ["0002"]


def _resolution_columns(url: str) -> list[str]:
    engine = db.create_engine(url)
    columns = [column["name"] for column in inspect(engine).get_columns("watchlist_resolution")]
    engine.dispose()
    return columns


def test_0004_adds_inactive_run_id_with_its_foreign_key_reruns_and_downgrades(url: str) -> None:
    db.upgrade(url, "0003")
    assert "inactive_run_id" not in _resolution_columns(url)

    db.upgrade(url)
    after = _unordered(_schema_sql(url))
    assert "inactive_run_id" in _resolution_columns(url)
    engine = db.create_engine(url)
    foreign_keys = inspect(engine).get_foreign_keys("watchlist_resolution")
    engine.dispose()
    assert {
        "name": "fk_watchlist_resolution_inactive_run_id_runs",
        "referred_table": "runs",
        "constrained_columns": ["inactive_run_id"],
        "referred_columns": ["run_id"],
    }.items() <= next(
        fk for fk in foreign_keys if fk["constrained_columns"] == ["inactive_run_id"]
    ).items()

    engine = db.create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0003'"))
    engine.dispose()
    db.upgrade(url)
    assert _unordered(_schema_sql(url)) == after
    assert _version(url) == ["0004"]

    db.downgrade(url, "0003")
    assert "inactive_run_id" not in _resolution_columns(url)
    assert _version(url) == ["0003"]
