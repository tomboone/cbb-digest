"""Command-line entry point (spec §9.1). Commands whose ticket has not landed are stubs."""

import logging
import signal
import threading
from collections.abc import Sized
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import FrameType
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from cbb_digest import db, liveness
from cbb_digest.clock import SystemClock
from cbb_digest.collect import (
    RequestCounter,
    cache_only_clients,
    collect,
    live_clients,
    teams_cached,
)
from cbb_digest.load import TABLES, LoadError, LoadInput, TableReport, stored_cbbd_teams
from cbb_digest.load import load as load_sources
from cbb_digest.logging import configure_logging
from cbb_digest.scheduler import Scheduler
from cbb_digest.schema import teams
from cbb_digest.settings import Settings
from cbb_digest.sources import cbbd
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.teams import build_teams, load_overrides
from cbb_digest.watchlist import Watchlist, WatchlistError, load_watchlist
from cbb_digest.weeks import EASTERN, monday_of, run_id_for, windows_for

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger(__name__)

# Plain aliases, not `type`: Typer cannot see through a TypeAliasType.
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Write out/digest.html and out/digest.txt; send nothing.")
]
ForceOption = Annotated[
    bool, typer.Option("--force", help="Send even if this run id has already been sent.")
]
WeekOption = Annotated[
    datetime | None,
    typer.Option("--week", formats=["%Y-%m-%d"], help="Any date in the week to run."),
]

CACHE_DIRNAME = "cache"


def _bootstrap() -> Settings:
    """Load the settings and configure logging; exit 2 if the configuration is invalid."""
    try:
        settings = Settings()
    except ValidationError as exc:
        configure_logging()
        log.error("invalid configuration", extra={"errors": exc.errors(include_url=False)})
        raise typer.Exit(2) from exc
    configure_logging(settings.log_level)
    return settings


def _load_watchlist() -> Watchlist:
    """The validated watchlist; exit 2 listing every error if it is invalid (spec §7.1).

    Called first, so a bad watchlist stops a run before anything is fetched.
    """
    try:
        return load_watchlist()
    except WatchlistError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _monday(week: datetime | None) -> date:
    """The Monday of `--week`, or of today in `America/New_York` when it is not given."""
    day = week.date() if week is not None else SystemClock().now().astimezone(EASTERN).date()
    return monday_of(day)


def _cache(settings: Settings) -> ResponseCache:
    return ResponseCache(settings.data_dir / CACHE_DIRNAME, SystemClock())


def _outcome(label: str, result: Fetched[Sized]) -> str:
    if isinstance(result, SourceFailure):
        return f"{label}: FAIL ({result.reason})"
    return f"{label}: OK ({len(result)} rows)"


def _table_line(table: str, report: TableReport) -> str:
    notes = ", ".join(f"{count} {reason}" for reason, count in sorted(report.notes.items()))
    return f"{table}: {report.changed} changed" + (f" ({notes})" if notes else "")


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"{command}: not implemented", err=True)
    raise typer.Exit(1)


@app.command()
def fetch(
    week: WeekOption = None,
    refresh_teams: Annotated[
        bool,
        typer.Option("--refresh-teams", help="Fetch CBBD's teams even if `teams` is filled."),
    ] = False,
) -> None:
    """Fetch every source for the run window into the 24 h cache."""
    season = _load_watchlist().season
    settings = _bootstrap()
    monday = _monday(week)
    engine = db.open_database(settings.data_dir)
    try:
        with engine.connect() as connection:
            teams_empty = connection.execute(select(teams.c.slug).limit(1)).first() is None
    finally:
        engine.dispose()

    counter = RequestCounter()
    clients = live_clients(settings.cbbd_api_key, counter)
    try:
        sources = collect(
            clients,
            _cache(settings),
            season=season,
            this_week_start=monday,
            include_teams=refresh_teams or teams_empty,
        )
    finally:
        clients.close()

    typer.echo(f"week of {monday.isoformat()}, run {run_id_for(monday, season)}")
    for label, result in sources.outcomes():
        typer.echo(_outcome(label, result))
    typer.echo(f"requests: {counter.count}")
    if any(isinstance(result, SourceFailure) for _, result in sources.outcomes()):
        raise typer.Exit(1)


@app.command()
def load(week: WeekOption = None) -> None:
    """Parse cached responses and upsert them into SQLite; snapshot the ratings."""
    season = _load_watchlist().season
    settings = _bootstrap()
    monday = _monday(week)
    run_id = run_id_for(monday, season)
    cache = _cache(settings)

    include_teams = teams_cached(cache, season)
    clients = cache_only_clients()
    try:
        sources = collect(
            clients, cache, season=season, this_week_start=monday, include_teams=include_teams
        )
    finally:
        clients.close()

    fetched_teams = sources.cbbd_teams
    if isinstance(fetched_teams, SourceFailure):
        fetched_teams = None
    ratings_entry = cache.peek(cbbd.SOURCE, cbbd.ratings_cache_key(season))
    data = LoadInput(
        season=season,
        run_id=run_id,
        windows=windows_for(monday),
        canonical_teams=None,
        player_season=sources.player_season,
        player_games=sources.player_games,
        ratings=sources.ratings,
        ratings_fetched_at=ratings_entry.fetched_at if ratings_entry is not None else None,
        espn_week=sources.espn_week,
    )

    engine = db.open_database(settings.data_dir)
    try:
        with engine.begin() as connection:
            # Re-apply teams_crosswalk.yaml every load, to the stored teams when none were fetched.
            cbbd_teams = fetched_teams or stored_cbbd_teams(connection)
            try:
                canonical = build_teams(cbbd_teams, load_overrides()) if cbbd_teams else None
            except ValueError as exc:
                # A crosswalk that would join the wrong team; fixed in the file, never here.
                typer.echo(f"teams_crosswalk.yaml: {exc}", err=True)
                raise typer.Exit(1) from exc
            report = load_sources(
                connection, replace(data, canonical_teams=canonical), now=SystemClock().now()
            )
    except LoadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()

    typer.echo(f"week of {monday.isoformat()}, run {run_id}")
    skipped = [(label, r) for label, r in sources.outcomes() if isinstance(r, SourceFailure)]
    for label, failure in skipped:
        typer.echo(f"{label}: skipped ({failure.reason})")
    for table in TABLES:
        if table in report.tables:
            typer.echo(_table_line(table, report.tables[table]))
    if skipped:
        raise typer.Exit(1)


@app.command()
def resolve() -> None:
    """Resolve the watchlist against the Torvik player season file."""
    _load_watchlist()
    _not_implemented("resolve")


@app.command()
def compute() -> None:
    """Build the digest model from the database."""
    _not_implemented("compute")


@app.command()
def render(dry_run: DryRunOption = False) -> None:
    """Render the digest to HTML and text."""
    _not_implemented("render")


@app.command()
def send(force: ForceOption = False) -> None:
    """Send the rendered digest through the transport chain."""
    _not_implemented("send")


@app.command()
def run(
    week: WeekOption = None,
    force: ForceOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Fetch, load, resolve, compute, render and send one week, then ping healthchecks."""
    _load_watchlist()
    _not_implemented("run")


@app.command()
def serve() -> None:
    """Run the weekly scheduler loop (container entrypoint)."""
    settings = _bootstrap()
    engine = db.open_database(settings.data_dir)
    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    # Docker stops a container with SIGTERM; wake from the current sleep and exit 0.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, on_signal)
    try:
        Scheduler(SystemClock(stop), engine, stop=stop).run_forever()
    finally:
        engine.dispose()
    log.info("scheduler stopped")


@app.command()
def reset(
    season: Annotated[int, typer.Option("--season", help="The new season's end year.")],
    send: Annotated[
        bool, typer.Option("--send", help="Email the reset digest instead of writing out/.")
    ] = False,
) -> None:
    """Build the portal-reset digest and a proposed watchlist for a new season."""
    _not_implemented("reset")


@app.command()
def health() -> None:
    """Exit 0 if the serve loop's heartbeat is under 8 days old, else 1."""
    try:
        settings = _bootstrap()
    except typer.Exit as exc:
        # Docker reserves health-check exit code 2; a config `serve` cannot load is unhealthy.
        raise typer.Exit(1) from exc
    # Read the database as it is: a health check must never create or migrate it.
    path = settings.data_dir / db.DATABASE_FILENAME
    if not path.is_file():
        typer.echo(f"unhealthy: no database at {path}")
        raise typer.Exit(1)
    engine = db.create_engine(db.database_url(settings.data_dir))
    try:
        ticked_at = liveness.last_tick(engine)
    except OperationalError as exc:
        # No heartbeat table (`serve` has not migrated yet), or a database that cannot be read.
        typer.echo(f"unhealthy: cannot read the heartbeat: {exc.orig}")
        raise typer.Exit(1) from exc
    finally:
        engine.dispose()
    if ticked_at is None:
        typer.echo("unhealthy: serve has never ticked")
        raise typer.Exit(1)
    now = SystemClock().now()
    if not liveness.is_healthy(ticked_at, now):
        typer.echo(
            f"unhealthy: serve last ticked {(now - ticked_at) / timedelta(days=1):.1f} days ago"
        )
        raise typer.Exit(1)
    typer.echo(f"healthy: serve last ticked at {ticked_at.isoformat()}")
