"""Alembic environment. `cbb_digest.db.upgrade` passes an open connection in
`config.attributes["connection"]`; the `alembic` CLI (see `alembic.ini`) builds one from
`sqlalchemy.url` instead.
"""

from typing import Any

from alembic import context
from sqlalchemy import Connection

from cbb_digest.db import create_engine
from cbb_digest.schema import metadata

config = context.config


def _include_name(name: str | None, type_: str, _parent_names: Any) -> bool:
    # A batch-mode rebuild that failed can leave `_alembic_tmp_<table>` behind; never let
    # autogenerate turn one into a stray drop_table.
    return not (type_ == "table" and name is not None and name.startswith("_alembic_tmp_"))


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=metadata,
        render_as_batch=True,
        include_name=_include_name,
        **kwargs,
    )


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection: Connection | None = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    assert url is not None, "set sqlalchemy.url in alembic.ini"
    engine = create_engine(url, foreign_keys=False)
    try:
        with engine.begin() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
