"""Add `watchlist_resolution.inactive_run_id`: the run that raised a standing `inactive` alert.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01

`inactive` is raised by the compute step from the game logs, not by resolution, and `resolve`
rewrites `alert_kind` on every run. Keeping the inactive alert in its own column lets it follow
the same on-change rule as the others (spec §7.4): raised once, when the player first becomes
inactive, and again only after they have played in between.

Reviewed by hand: no stray batch temp-table statements, and safe to run twice (spec §9.4). The
column, with its named foreign key, is added only when missing and dropped only when present.
This file must not import `cbb_digest.schema`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "watchlist_resolution"
COLUMN = "inactive_run_id"
FOREIGN_KEY = "fk_watchlist_resolution_inactive_run_id_runs"


def _present() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(TABLE)
    return any(column["name"] == COLUMN for column in columns)


def upgrade() -> None:
    if _present():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(), nullable=True))
        batch.create_foreign_key(op.f(FOREIGN_KEY), "runs", [COLUMN], ["run_id"])


def downgrade() -> None:
    if not _present():
        return
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(op.f(FOREIGN_KEY), type_="foreignkey")
        batch.drop_column(COLUMN)
