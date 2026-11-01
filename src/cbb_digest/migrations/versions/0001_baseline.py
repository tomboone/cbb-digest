"""Baseline: every table in spec §9.4.

Revision ID: 0001
Revises:
Create Date: 2026-10-31

Reviewed by hand: no stray batch temp-table statements, and safe to run twice (spec §9.4).
Every create is `IF NOT EXISTS` and every drop `IF EXISTS`, so re-running over a database that
already has some of the tables completes rather than failing. Names are `op.f()` literals matching
`cbb_digest.schema.NAMING_CONVENTION`; this file must not import `cbb_digest.schema`, whose
shape moves on with later migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_KINDS = "kind IN ('digest', 'reset')"
RUN_STATUSES = (
    "status IN ('running', 'sent', 'sent_degraded', 'skipped_offseason', 'skipped_missed', "
    "'failed')"
)
RESOLUTION_OUTCOMES = "outcome IN ('resolved', 'unresolved', 'ambiguous')"
RESOLUTION_ALERT_KINDS = "alert_kind IN ('unresolved', 'ambiguous', 'team_changed', 'fuzzy_match')"

TABLES = (
    "teams",
    "runs",
    "team_ratings_snapshot",
    "players",
    "player_games",
    "games",
    "watchlist_resolution",
    "heartbeat",
)


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("cbbd_id", sa.Integer(), nullable=False),
        sa.Column("cbbd_name", sa.String(), nullable=False),
        sa.Column("torvik_name", sa.String(), nullable=True),
        sa.Column("espn_id", sa.String(), nullable=True),
        sa.Column("espn_name", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("slug", name=op.f("pk_teams")),
        sa.UniqueConstraint("cbbd_id", name=op.f("uq_teams_cbbd_id")),
        sa.UniqueConstraint("torvik_name", name=op.f("uq_teams_torvik_name")),
        sa.UniqueConstraint("espn_id", name=op.f("uq_teams_espn_id")),
        if_not_exists=True,
    )
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("last_week_start", sa.DateTime(), nullable=True),
        sa.Column("last_week_end", sa.DateTime(), nullable=True),
        sa.Column("this_week_start", sa.DateTime(), nullable=True),
        sa.Column("this_week_end", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("row_counts", sa.JSON(), nullable=True),
        sa.Column("failed_sources", sa.JSON(), nullable=True),
        sa.Column("alerts", sa.JSON(), nullable=True),
        sa.Column("image_sha", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("transport", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
        sa.CheckConstraint(RUN_KINDS, name=op.f("ck_runs_kind")),
        sa.CheckConstraint(RUN_STATUSES, name=op.f("ck_runs_status")),
        if_not_exists=True,
    )
    op.create_table(
        "team_ratings_snapshot",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("net", sa.Float(), nullable=False),
        sa.Column("adj_o", sa.Float(), nullable=False),
        sa.Column("adj_d", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "slug", name=op.f("pk_team_ratings_snapshot")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_team_ratings_snapshot_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["slug"], ["teams.slug"], name=op.f("fk_team_ratings_snapshot_slug_teams")
        ),
        if_not_exists=True,
    )
    op.create_table(
        "players",
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("torvik_pid", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("team_slug", sa.String(), nullable=False),
        sa.Column("class_year", sa.String(), nullable=True),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("min", sa.Float(), nullable=False),
        sa.Column("pts", sa.Integer(), nullable=False),
        sa.Column("reb", sa.Integer(), nullable=False),
        sa.Column("ast", sa.Integer(), nullable=False),
        sa.Column("fga", sa.Integer(), nullable=False),
        sa.Column("fta", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("season", "torvik_pid", name=op.f("pk_players")),
        sa.ForeignKeyConstraint(
            ["team_slug"], ["teams.slug"], name=op.f("fk_players_team_slug_teams")
        ),
        if_not_exists=True,
    )
    op.create_table(
        "player_games",
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("torvik_pid", sa.Integer(), nullable=False),
        sa.Column("game_date", sa.Date(), nullable=False),
        sa.Column("opponent_slug", sa.String(), nullable=False),
        sa.Column("min", sa.Float(), nullable=False),
        sa.Column("pts", sa.Integer(), nullable=False),
        sa.Column("reb", sa.Integer(), nullable=False),
        sa.Column("ast", sa.Integer(), nullable=False),
        sa.Column("fgm", sa.Integer(), nullable=False),
        sa.Column("fga", sa.Integer(), nullable=False),
        sa.Column("fg3m", sa.Integer(), nullable=False),
        sa.Column("fg3a", sa.Integer(), nullable=False),
        sa.Column("fta", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "season", "torvik_pid", "game_date", "opponent_slug", name=op.f("pk_player_games")
        ),
        sa.ForeignKeyConstraint(
            ["season", "torvik_pid"],
            ["players.season", "players.torvik_pid"],
            name=op.f("fk_player_games_season_torvik_pid_players"),
        ),
        if_not_exists=True,
    )
    op.create_table(
        "games",
        sa.Column("espn_id", sa.String(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("start_utc", sa.DateTime(), nullable=False),
        sa.Column("home_slug", sa.String(), nullable=True),
        sa.Column("away_slug", sa.String(), nullable=True),
        sa.Column("networks", sa.JSON(), nullable=False),
        sa.Column("venue", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("espn_id", name=op.f("pk_games")),
        sa.ForeignKeyConstraint(
            ["home_slug"], ["teams.slug"], name=op.f("fk_games_home_slug_teams")
        ),
        sa.ForeignKeyConstraint(
            ["away_slug"], ["teams.slug"], name=op.f("fk_games_away_slug_teams")
        ),
        if_not_exists=True,
    )
    op.create_table(
        "watchlist_resolution",
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("watchlist_key", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("torvik_pid", sa.Integer(), nullable=True),
        sa.Column("team_slug", sa.String(), nullable=True),
        sa.Column("matched_name", sa.String(), nullable=True),
        sa.Column("alert_kind", sa.String(), nullable=True),
        sa.Column("alert_detail", sa.Text(), nullable=True),
        sa.Column("first_run_id", sa.String(), nullable=False),
        sa.Column("changed_run_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("season", "watchlist_key", name=op.f("pk_watchlist_resolution")),
        sa.ForeignKeyConstraint(
            ["team_slug"], ["teams.slug"], name=op.f("fk_watchlist_resolution_team_slug_teams")
        ),
        sa.ForeignKeyConstraint(
            ["first_run_id"],
            ["runs.run_id"],
            name=op.f("fk_watchlist_resolution_first_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["changed_run_id"],
            ["runs.run_id"],
            name=op.f("fk_watchlist_resolution_changed_run_id_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["season", "torvik_pid"],
            ["players.season", "players.torvik_pid"],
            name=op.f("fk_watchlist_resolution_season_torvik_pid_players"),
        ),
        sa.CheckConstraint(RESOLUTION_OUTCOMES, name=op.f("ck_watchlist_resolution_outcome")),
        sa.CheckConstraint(RESOLUTION_ALERT_KINDS, name=op.f("ck_watchlist_resolution_alert_kind")),
        if_not_exists=True,
    )
    op.create_table(
        "heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticked_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_heartbeat")),
        sa.CheckConstraint("id = 1", name=op.f("ck_heartbeat_singleton")),
        if_not_exists=True,
    )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table, if_exists=True)
