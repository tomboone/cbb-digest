"""The `Digest`: the four section builders and this run's alerts brought together, with a failed
source turned into a banner line and omitted sections rather than a crash (spec §5, §9.3).

A source that failed this run omits every section that depends on it:

- **ESPN:** featured games.
- **Torvik:** watchlist last week and freshmen. Resolution was skipped, so featured games use the
  rows the last successful run stored, and the banner says so.
- **CBBD:** movers and freshmen, and every team in featured games is unranked.

Freshmen go with CBBD too, because their top-100 filter is this run's ranks (§5 lists CBBD among
the section's sources). A section is never filled from an earlier week's data in place of a failed
source's: that is the line §9.3 draws.
"""

from collections.abc import Collection
from datetime import date, datetime
from typing import Literal

from sqlalchemy import Connection, select

from cbb_digest.collect import Sources
from cbb_digest.compute.freshmen import freshmen_to_watch
from cbb_digest.compute.games import featured_games
from cbb_digest.compute.models import Digest, Footer
from cbb_digest.compute.movers import movers
from cbb_digest.compute.watchlist_week import watchlist_last_week
from cbb_digest.resolve import raised_alerts
from cbb_digest.schema import runs
from cbb_digest.sources.fetched import SourceFailure
from cbb_digest.watchlist import Watchlist
from cbb_digest.weeks import EASTERN, run_id_for

type Source = Literal["espn", "torvik", "cbbd"]

# In the order the banner lists them, which is the order of the sections they omit.
BANNER: dict[Source, str] = {
    "espn": "ESPN schedule unavailable this week — featured games omitted.",
    "torvik": (
        "Torvik stats unavailable this week — watchlist and freshmen omitted; "
        "player matches are from the last successful run."
    ),
    "cbbd": (
        "CBBD ratings unavailable this week — movers and freshmen omitted; teams shown unranked."
    ),
}

REPO_URL = "https://github.com/tomboone/cbb-digest"
DEFAULT_REF = "main"


def failed_sources(sources: Sources) -> frozenset[Source]:
    """The sources whose data this run lacks. CBBD's teams do not count: they feed the
    crosswalk, which the stored teams stand in for, and no section."""
    failed: set[Source] = set()
    if isinstance(sources.espn_week, SourceFailure):
        failed.add("espn")
    if isinstance(sources.player_season, SourceFailure) or isinstance(
        sources.player_games, SourceFailure
    ):
        failed.add("torvik")
    if isinstance(sources.ratings, SourceFailure):
        failed.add("cbbd")
    return frozenset(failed)


def subject_for(monday: date) -> str:
    """`CBB digest — week of Nov 9` (§5)."""
    return f"CBB digest — week of {monday:%b} {monday.day}"


def watchlist_url(image_sha: str | None) -> str:
    """`watchlist.yaml` on GitHub at the image's commit, or on `main` without one."""
    return f"{REPO_URL}/blob/{image_sha or DEFAULT_REF}/watchlist.yaml"


def build_digest(
    connection: Connection,
    watchlist: Watchlist,
    monday: date,
    *,
    failed: Collection[Source],
    now: datetime,
    image_sha: str | None,
) -> Digest:
    """The digest for the week of `monday`, from what `load` and `resolve` stored.

    `failed` names the sources this run lacks. Run it inside the run's transaction: the
    watchlist section stores each player's inactive state as it goes. Raises `LookupError` when
    the run, or a section's ratings snapshot, has not been loaded; `ValueError` when the run is
    in another season; `ResolveError` when `teams` is empty.
    """
    run_id = run_id_for(monday, watchlist.season)
    last_day = _last_day(connection, run_id, watchlist.season)
    espn_ok = "espn" not in failed
    torvik_ok = "torvik" not in failed
    cbbd_ok = "cbbd" not in failed

    featured = featured_games(connection, run_id, watchlist, ranked=cbbd_ok) if espn_ok else None
    week = watchlist_last_week(connection, run_id, watchlist) if torvik_ok else None
    freshmen = freshmen_to_watch(connection, run_id, watchlist) if torvik_ok and cbbd_ok else None
    moved = movers(connection, run_id, watchlist.season) if cbbd_ok else None

    return Digest(
        run_id=run_id,
        subject=subject_for(monday),
        week_of=monday,
        banner=[line for source, line in BANNER.items() if source in failed],
        featured=featured,
        # Its `inactive` alerts move to `alerts`, so the digest holds each alert once.
        watchlist=week.model_copy(update={"alerts": []}) if week is not None else None,
        freshmen=freshmen,
        movers=moved,
        alerts=[
            *raised_alerts(connection, watchlist, run_id),
            *(week.alerts if week is not None else []),
        ],
        footer=Footer(
            run_ts_et=now.astimezone(EASTERN),
            data_as_of=last_day,
            run_id=run_id,
            image_sha=image_sha,
            watchlist_url=watchlist_url(image_sha),
        ),
    )


def _last_day(connection: Connection, run_id: str, season: int) -> date:
    """The last day of the run's last week, Eastern; checks the run exists in `season`."""
    run = connection.execute(
        select(runs.c.season, runs.c.last_week_end).where(runs.c.run_id == run_id)
    ).one_or_none()
    if run is None or run.last_week_end is None:
        raise LookupError(f"no digest run {run_id!r} with windows: run `cbb-digest load` first")
    if run.season != season:
        raise ValueError(f"run {run_id!r} is in season {run.season}, not {season}")
    return run.last_week_end.astimezone(EASTERN).date()
