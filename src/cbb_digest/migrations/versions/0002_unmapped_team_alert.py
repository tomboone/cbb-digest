"""Allow `unmapped_team` as a `watchlist_resolution.alert_kind`.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01

A watchlist player whose stated team the crosswalk cannot map is stored like any other entry, so
its `unmapped_team` alert is raised once rather than on every run (spec §7.4).

Reviewed by hand: no stray batch temp-table statements, and safe to run twice (spec §9.4). SQLite
cannot alter a CHECK constraint, so the batch rebuilds the table, dropping the constraint by name
and adding it back with the new list; a second run rebuilds it identically. The downgrade first
deletes the rows the old constraint would refuse. This file must not import `cbb_digest.schema`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "alert_kind"
OLD_KINDS = "alert_kind IN ('unresolved', 'ambiguous', 'team_changed', 'fuzzy_match')"
NEW_KINDS = (
    "alert_kind IN ('unresolved', 'ambiguous', 'team_changed', 'fuzzy_match', 'unmapped_team')"
)


def _replace_check(condition: str) -> None:
    with op.batch_alter_table("watchlist_resolution", recreate="always") as batch:
        batch.drop_constraint(op.f(f"ck_watchlist_resolution_{CONSTRAINT}"), type_="check")
        batch.create_check_constraint(op.f(f"ck_watchlist_resolution_{CONSTRAINT}"), condition)


def upgrade() -> None:
    _replace_check(NEW_KINDS)


def downgrade() -> None:
    op.execute("DELETE FROM watchlist_resolution WHERE alert_kind = 'unmapped_team'")
    _replace_check(OLD_KINDS)
