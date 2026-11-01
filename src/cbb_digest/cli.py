"""Command-line entry point (spec §9.1). Commands whose ticket has not landed are stubs."""

import logging
import signal
import threading
from datetime import datetime, timedelta
from types import FrameType
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from cbb_digest import db, liveness
from cbb_digest.clock import SystemClock
from cbb_digest.logging import configure_logging
from cbb_digest.scheduler import Scheduler
from cbb_digest.settings import Settings
from cbb_digest.watchlist import Watchlist, WatchlistError, load_watchlist

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger(__name__)

# Plain aliases, not `type`: Typer cannot see through a TypeAliasType.
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Write out/digest.html and out/digest.txt; send nothing.")
]
ForceOption = Annotated[
    bool, typer.Option("--force", help="Send even if this run id has already been sent.")
]


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


def _not_implemented(command: str) -> NoReturn:
    typer.echo(f"{command}: not implemented", err=True)
    raise typer.Exit(1)


@app.command()
def fetch() -> None:
    """Fetch every source for the run window into the 24 h cache."""
    _not_implemented("fetch")


@app.command()
def load() -> None:
    """Parse cached responses and upsert them into SQLite; snapshot the ratings."""
    _not_implemented("load")


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
    week: Annotated[
        datetime | None,
        typer.Option("--week", formats=["%Y-%m-%d"], help="Any date in the week to run."),
    ] = None,
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
