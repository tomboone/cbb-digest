"""Every table in the SQLite database (spec §9.4), as SQLAlchemy core `Table` objects.

This module is the current shape of the schema; the migrations under `migrations/versions/` are
how a database gets there. `tests/test_migrations.py` fails if the two disagree.

Team columns are slugs from `teams`. A source team name with no crosswalk entry never reaches a
table as a slug (spec §7.4); an ESPN game against such a team stores NULL for that side.
`player_games.opponent_slug` is part of the key and not a foreign key. Torvik's game logs hold
only D-I vs D-I games (§6), so the loader drops a game whose opponent the crosswalk cannot map,
as it drops any unmapped row: that is a crosswalk gap (`unmapped_team`), not a non-D-I opponent.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)
from sqlalchemy.types import TypeDecorator

# Named constraints, so a later batch migration can find a constraint SQLite would otherwise
# leave anonymous. The baseline migration spells out the same names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UTCDateTime(TypeDecorator[datetime]):
    """An aware datetime stored as naive UTC, and read back as aware UTC.

    SQLite has no time-zone type, and SQLAlchemy drops the offset of an aware datetime without
    converting it, so an Eastern time would be stored as if it were UTC. A naive datetime is
    rejected rather than guessed at.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: store an aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        assert isinstance(value, datetime)
        return value.replace(tzinfo=UTC)


RUN_KINDS = ("digest", "reset")
RUN_STATUSES = ("running", "sent", "sent_degraded", "skipped_offseason", "skipped_missed", "failed")
RESOLUTION_OUTCOMES = ("resolved", "unresolved", "ambiguous")
RESOLUTION_ALERT_KINDS = ("unresolved", "ambiguous", "team_changed", "fuzzy_match")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _team_slug(name: str, *, nullable: bool) -> Column[str]:
    return Column(name, String, ForeignKey("teams.slug"), nullable=nullable)


teams = Table(
    "teams",
    metadata,
    Column("slug", String, primary_key=True),
    Column("cbbd_id", Integer, nullable=False, unique=True),
    Column("cbbd_name", String, nullable=False),
    Column("torvik_name", String, unique=True),
    Column("espn_id", String, unique=True),
    Column("espn_name", String),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("season", Integer, nullable=False),
    # The §8.1 windows; NULL on a reset run, which has none.
    Column("last_week_start", UTCDateTime),
    Column("last_week_end", UTCDateTime),
    Column("this_week_start", UTCDateTime),
    Column("this_week_end", UTCDateTime),
    Column("status", String, nullable=False),
    Column("started_at", UTCDateTime, nullable=False),
    Column("finished_at", UTCDateTime),
    Column("duration_s", Float()),
    # {source: rows loaded}, [source, ...] and [alert, ...].
    Column("row_counts", JSON),
    Column("failed_sources", JSON),
    Column("alerts", JSON),
    Column("image_sha", String),
    Column("sent_at", UTCDateTime),
    Column("transport", String),
    CheckConstraint(_one_of("kind", RUN_KINDS), name="kind"),
    CheckConstraint(_one_of("status", RUN_STATUSES), name="status"),
)

team_ratings_snapshot = Table(
    "team_ratings_snapshot",
    metadata,
    Column("run_id", String, ForeignKey("runs.run_id"), nullable=False),
    _team_slug("slug", nullable=False),
    Column("rank", Integer, nullable=False),
    Column("net", Float(), nullable=False),
    Column("adj_o", Float(), nullable=False),
    Column("adj_d", Float(), nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
    PrimaryKeyConstraint("run_id", "slug"),
)

players = Table(
    "players",
    metadata,
    Column("season", Integer, nullable=False),
    Column("torvik_pid", Integer, nullable=False),
    Column("name", String, nullable=False),
    Column("normalized_name", String, nullable=False),
    _team_slug("team_slug", nullable=False),
    Column("class_year", String),
    # Season-to-date totals (§8.4 and §8.5); per-game figures are derived from these.
    Column("games", Integer, nullable=False),
    Column("min", Float(), nullable=False),
    Column("pts", Integer, nullable=False),
    Column("reb", Integer, nullable=False),
    Column("ast", Integer, nullable=False),
    Column("fga", Integer, nullable=False),
    Column("fta", Integer, nullable=False),
    PrimaryKeyConstraint("season", "torvik_pid"),
)

player_games = Table(
    "player_games",
    metadata,
    Column("season", Integer, nullable=False),
    Column("torvik_pid", Integer, nullable=False),
    Column("game_date", Date, nullable=False),
    Column("opponent_slug", String, nullable=False),
    Column("min", Float(), nullable=False),
    Column("pts", Integer, nullable=False),
    Column("reb", Integer, nullable=False),
    Column("ast", Integer, nullable=False),
    Column("fgm", Integer, nullable=False),
    Column("fga", Integer, nullable=False),
    Column("fg3m", Integer, nullable=False),
    Column("fg3a", Integer, nullable=False),
    Column("fta", Integer, nullable=False),
    PrimaryKeyConstraint("season", "torvik_pid", "game_date", "opponent_slug"),
    ForeignKeyConstraint(
        ["season", "torvik_pid"],
        ["players.season", "players.torvik_pid"],
        name="fk_player_games_season_torvik_pid_players",
    ),
)

games = Table(
    "games",
    metadata,
    Column("espn_id", String, primary_key=True),
    Column("season", Integer, nullable=False),
    Column("start_utc", UTCDateTime, nullable=False),
    # NULL when that side is not a canonical team (spec §8.3 ranks it 999).
    _team_slug("home_slug", nullable=True),
    _team_slug("away_slug", nullable=True),
    # ESPN `broadcasts[].names`, in order; [] when the game has no broadcast.
    Column("networks", JSON, nullable=False),
    Column("venue", String),
)

watchlist_resolution = Table(
    "watchlist_resolution",
    metadata,
    Column("season", Integer, nullable=False),
    # Normalised name + stated team slug, or `—` when the team is blank (§7.3).
    Column("watchlist_key", String, nullable=False),
    Column("outcome", String, nullable=False),
    Column("torvik_pid", Integer),
    _team_slug("team_slug", nullable=True),
    Column("matched_name", String),
    Column("alert_kind", String),
    # What the alert says beyond its kind — the old and new team, the fuzzy match, the
    # candidates — so a change in details raises the alert again (§7.4).
    Column("alert_detail", Text),
    Column("first_run_id", String, ForeignKey("runs.run_id"), nullable=False),
    Column("changed_run_id", String, ForeignKey("runs.run_id"), nullable=False),
    PrimaryKeyConstraint("season", "watchlist_key"),
    ForeignKeyConstraint(
        ["season", "torvik_pid"],
        ["players.season", "players.torvik_pid"],
        name="fk_watchlist_resolution_season_torvik_pid_players",
    ),
    CheckConstraint(_one_of("outcome", RESOLUTION_OUTCOMES), name="outcome"),
    CheckConstraint(_one_of("alert_kind", RESOLUTION_ALERT_KINDS), name="alert_kind"),
)

heartbeat = Table(
    "heartbeat",
    metadata,
    # A singleton: the only row has id 1.
    Column("id", Integer, primary_key=True),
    Column("ticked_at", UTCDateTime, nullable=False),
    CheckConstraint("id = 1", name="singleton"),
)
