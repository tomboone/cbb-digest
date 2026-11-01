"""Whether a Monday is in season, from the data alone (spec §8.2).

A Monday is in season if Torvik's game logs hold a game dated within last week, or ESPN's
scoreboard holds a D-I game within this week. No dates are configured: the first send is the
Monday of opening week, which has games this week only, and the last is the Monday after the
title game, which has games last week only.

The answer comes from `player_games` and `games` after `load`, and a source counts only if this
run fetched it. A table whose fetch failed may still hold rows from an earlier fetch of the same
week, and §9.3 never lets stale data stand in. So:
- a fetched source that finds a game makes the Monday in season;
- otherwise, if either source was fetched, it is out of season;
- with both fetches failed the gate is unknown (`None`). The run goes ahead, and the failure
  banner explains the gaps.

One failed source therefore leaves the other to decide alone. A summer Monday whose Torvik fetch
hits the browser check is still skipped, not sent as an empty digest. The cost is the two edge
Mondays: the opening-week digest needs ESPN, and the post-title recap needs Torvik.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from sqlalchemy import Connection, exists, select

from cbb_digest.runs import finish_run
from cbb_digest.schema import games, player_games
from cbb_digest.weeks import windows_for


def is_in_season(
    connection: Connection, monday: date, *, game_logs_fetched: bool, espn_fetched: bool
) -> bool | None:
    """Whether the run on `monday` is in season, from the sources it fetched; `None` if neither."""
    windows = windows_for(monday)
    verdicts: list[bool] = []
    if game_logs_fetched:
        last_week = player_games.c.game_date.between(
            windows.last_week.start.date(), windows.last_week.end.date()
        )
        verdicts.append(connection.execute(select(exists().where(last_week))).scalar_one())
    if espn_fetched:
        # Every row is a D-I game: the scoreboard is D-I only (`groups=50`), and a NULL side is
        # a non-D-I opponent or a crosswalk gap, not a reason to doubt the game.
        this_week = games.c.start_utc.between(windows.this_week.start, windows.this_week.end)
        verdicts.append(connection.execute(select(exists().where(this_week))).scalar_one())
    if not verdicts:
        return None
    return any(verdicts)


def finish_if_offseason(
    connection: Connection,
    *,
    run_id: str,
    monday: date,
    game_logs_fetched: bool,
    espn_fetched: bool,
    row_counts: Mapping[str, int],
    failed_sources: Sequence[str],
    now: datetime,
) -> bool:
    """Finish `run_id` as `skipped_offseason` and return True if `monday` is out of season.

    Called after `load` and before `compute`, so an out-of-season run computes and sends nothing.
    An unknown gate returns False: the run proceeds.
    """
    in_season = is_in_season(
        connection, monday, game_logs_fetched=game_logs_fetched, espn_fetched=espn_fetched
    )
    if in_season is not False:
        return False
    finish_run(
        connection,
        run_id=run_id,
        status="skipped_offseason",
        row_counts=row_counts,
        failed_sources=failed_sources,
        alerts=[],
        transport=None,
        now=now,
    )
    return True
