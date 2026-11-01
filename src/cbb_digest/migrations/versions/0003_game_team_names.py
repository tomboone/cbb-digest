"""Add `games.home_name` and `games.away_name`: ESPN's name for each side.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01

A featured game against a team the crosswalk does not know, such as a non-D-I opponent, shows
that team under its ESPN name (spec §7.5). Such a side stores a NULL slug, so without these
columns the name is lost at load time.

Reviewed by hand: no stray batch temp-table statements, and safe to run twice (spec §9.4). Each
column is added only when missing and dropped only when present. Existing rows keep NULL until
the next `load` of their week fills them. This file must not import `cbb_digest.schema`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "games"
COLUMNS = ("home_name", "away_name")


def _existing() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    missing = [name for name in COLUMNS if name not in _existing()]
    if not missing:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in missing:
            batch.add_column(sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    present = [name for name in COLUMNS if name in _existing()]
    if not present:
        return
    with op.batch_alter_table(TABLE) as batch:
        for name in present:
            batch.drop_column(name)
