"""Command-line entry point (spec §9.1). Commands whose ticket has not landed are stubs.

Each step's work lives in a helper that both its own command and `run` call, so a step run alone
does exactly what it does inside `run`.
"""

import logging
import signal
import threading
from collections.abc import Collection, Sized
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError

from cbb_digest import db, liveness
from cbb_digest.atomic import write_atomic
from cbb_digest.clock import SystemClock
from cbb_digest.collect import (
    RequestCounter,
    Sources,
    cache_only_clients,
    collect,
    live_clients,
    teams_cached,
)
from cbb_digest.compute.build import Source, build_digest, failed_sources
from cbb_digest.compute.models import Digest
from cbb_digest.load import (
    TABLES,
    LoadError,
    LoadInput,
    LoadReport,
    TableReport,
    stored_cbbd_teams,
)
from cbb_digest.load import load as load_sources
from cbb_digest.logging import configure_logging
from cbb_digest.render.render import Rendered
from cbb_digest.render.render import render as render_digest
from cbb_digest.resolve import Entry, ResolveError, ResolveReport
from cbb_digest.resolve import resolve as resolve_watchlist
from cbb_digest.runs import (
    FinalStatus,
    already_sent,
    finish_run,
    has_run,
    record_send,
    start_run,
)
from cbb_digest.scheduler import Scheduler
from cbb_digest.schema import teams
from cbb_digest.season_gate import finish_if_offseason
from cbb_digest.settings import Settings
from cbb_digest.sources import cbbd
from cbb_digest.sources.cache import ResponseCache
from cbb_digest.sources.fetched import Fetched, SourceFailure
from cbb_digest.teams import build_teams, load_overrides
from cbb_digest.transport.base import Message, SendReceipt, Transport, TransportError
from cbb_digest.transport.factory import TransportConfigError, build_transport
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
OUT_DIRNAME = "out"
DIGEST_JSON = "digest.json"
DIGEST_HTML = "digest.html"
DIGEST_TEXT = "digest.txt"


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


def _teams_empty(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.execute(select(teams.c.slug).limit(1)).first() is None


def _fetch_sources(
    settings: Settings, engine: Engine, season: int, monday: date, *, refresh_teams: bool
) -> tuple[Sources, int]:
    """Every source for the week of `monday`, over the network into the cache, and the number
    of requests that took. CBBD's teams come too when asked for or when `teams` is empty."""
    include_teams = refresh_teams or _teams_empty(engine)
    counter = RequestCounter()
    clients = live_clients(settings.cbbd_api_key, counter)
    try:
        sources = collect(
            clients,
            _cache(settings),
            season=season,
            this_week_start=monday,
            include_teams=include_teams,
        )
    finally:
        clients.close()
    return sources, counter.count


def _echo_fetched(sources: Sources, requests: int) -> None:
    for label, result in sources.outcomes():
        typer.echo(_outcome(label, result))
    typer.echo(f"requests: {requests}")


def _load_week(
    settings: Settings, engine: Engine, season: int, monday: date, sources: Sources
) -> LoadReport:
    """Upsert `sources` into SQLite and snapshot the ratings for the week's run id.

    Exits 1 when `teams_crosswalk.yaml` would join the wrong team, or the load cannot proceed.
    """
    fetched_teams = sources.cbbd_teams
    if isinstance(fetched_teams, SourceFailure):
        fetched_teams = None
    ratings_entry = _cache(settings).peek(cbbd.SOURCE, cbbd.ratings_cache_key(season))
    data = LoadInput(
        season=season,
        run_id=run_id_for(monday, season),
        windows=windows_for(monday),
        canonical_teams=None,
        player_season=sources.player_season,
        player_games=sources.player_games,
        ratings=sources.ratings,
        ratings_fetched_at=ratings_entry.fetched_at if ratings_entry is not None else None,
        espn_week=sources.espn_week,
    )
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
            return load_sources(
                connection, replace(data, canonical_teams=canonical), now=SystemClock().now()
            )
    except LoadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _echo_loaded(report: LoadReport) -> None:
    for table in TABLES:
        if table in report.tables:
            typer.echo(_table_line(table, report.tables[table]))


def _resolve_week(engine: Engine, wl: Watchlist, monday: date) -> ResolveReport:
    """Resolve the watchlist for the week of `monday`; exits 1 when `teams` is empty."""
    try:
        with engine.begin() as connection:
            return resolve_watchlist(
                connection,
                wl,
                run_id=run_id_for(monday, wl.season),
                windows=windows_for(monday),
                now=SystemClock().now(),
            )
    except ResolveError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _compute_digest(
    settings: Settings, engine: Engine, wl: Watchlist, monday: date, failed: Collection[Source]
) -> Digest:
    """The week's digest, written to out/digest.json; exits 1 when it cannot be built."""
    try:
        with engine.begin() as connection:
            digest = build_digest(
                connection,
                wl,
                monday,
                failed=failed,
                now=SystemClock().now(),
                image_sha=settings.image_sha,
            )
    except (LookupError, ValueError, ResolveError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    write_atomic(_out(settings) / DIGEST_JSON, digest.model_dump_json(indent=2) + "\n")
    return digest


def _out(settings: Settings) -> Path:
    return settings.data_dir / OUT_DIRNAME


def _read_digest(settings: Settings, wl: Watchlist, week: datetime | None) -> Digest:
    """out/digest.json, which `compute` wrote; exits 1 when it is missing, unreadable, or for
    another week than `week`."""
    path = _out(settings) / DIGEST_JSON
    expected = run_id_for(_monday(week), wl.season)
    try:
        digest = Digest.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        typer.echo(f"no {path}: run `cbb-digest compute` first", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo(f"{path} is not a digest: run `cbb-digest compute` again", err=True)
        raise typer.Exit(1) from exc
    if digest.run_id != expected:
        typer.echo(
            f"{path} is run {digest.run_id}, not {expected}: "
            "run `cbb-digest compute` for that week first",
            err=True,
        )
        raise typer.Exit(1)
    return digest


def _render_digest(settings: Settings, digest: Digest) -> Rendered:
    """The digest rendered, and written to out/digest.html and out/digest.txt; exits 1 when its
    MJML does not compile."""
    try:
        rendered = render_digest(digest)
    except ValueError as exc:
        typer.echo(f"the digest's MJML did not compile: {exc}", err=True)
        raise typer.Exit(1) from exc
    out = _out(settings)
    for name, body in ((DIGEST_HTML, rendered.html), (DIGEST_TEXT, rendered.text)):
        write_atomic(out / name, body)
        typer.echo(f"wrote {out / name}")
    return rendered


def _transport(settings: Settings, run_id: str) -> Transport:
    """The transport this send goes through; exits 2 when none is configured.

    Built before anything is fetched, so a configuration that could never send stops the run at
    once rather than after the work.
    """
    try:
        return build_transport(settings, run_id=run_id)
    except TransportConfigError as exc:
        typer.echo(f"invalid configuration: {exc}", err=True)
        raise typer.Exit(2) from exc


def _refuse_if_sent(engine: Engine, settings: Settings, run_id: str, *, force: bool) -> None:
    """Exit 1 when `run_id` has already gone out by email, unless `force` or this is a dry run,
    which emails nobody (spec §9.2)."""
    if force or settings.dry_run:
        return
    with engine.connect() as connection:
        sent = already_sent(connection, run_id)
    if sent:
        typer.echo(f"run {run_id} has already been sent; pass --force to send it again", err=True)
        raise typer.Exit(1)


def _send_digest(
    settings: Settings, engine: Engine, transport: Transport, run_id: str, rendered: Rendered
) -> SendReceipt:
    """Send the rendered digest to `RECIPIENTS` and stamp the run as sent; exits 1 when no
    transport delivers it."""
    message = Message(
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        to=settings.recipients,
        # Every real transport sends from its own configured sender; this is informational.
        from_=settings.resend_from or settings.smtp_sender_address or "",
    )
    try:
        receipt = transport.send(message)
    except TransportError as exc:
        log.error("send failed", extra={"run_id": run_id, "error": str(exc)})
        typer.echo(f"send failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    with engine.begin() as connection:
        record_send(connection, run_id=run_id, transport=receipt.transport, sent_at=receipt.sent_at)
    log.info(
        "digest sent",
        extra={"run_id": run_id, "transport": receipt.transport, "message_id": receipt.message_id},
    )
    typer.echo(f"sent via {receipt.transport} ({receipt.message_id})")
    return receipt


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
        sources, requests = _fetch_sources(
            settings, engine, season, monday, refresh_teams=refresh_teams
        )
    finally:
        engine.dispose()

    typer.echo(f"week of {monday.isoformat()}, run {run_id_for(monday, season)}")
    _echo_fetched(sources, requests)
    if any(isinstance(result, SourceFailure) for _, result in sources.outcomes()):
        raise typer.Exit(1)


@app.command()
def load(week: WeekOption = None) -> None:
    """Parse cached responses and upsert them into SQLite; snapshot the ratings."""
    season = _load_watchlist().season
    settings = _bootstrap()
    monday = _monday(week)
    cache = _cache(settings)

    include_teams = teams_cached(cache, season)
    clients = cache_only_clients()
    try:
        sources = collect(
            clients, cache, season=season, this_week_start=monday, include_teams=include_teams
        )
    finally:
        clients.close()

    engine = db.open_database(settings.data_dir)
    try:
        report = _load_week(settings, engine, season, monday, sources)
    finally:
        engine.dispose()

    typer.echo(f"week of {monday.isoformat()}, run {run_id_for(monday, season)}")
    skipped = [(label, r) for label, r in sources.outcomes() if isinstance(r, SourceFailure)]
    for label, failure in skipped:
        typer.echo(f"{label}: skipped ({failure.reason})")
    _echo_loaded(report)
    if skipped:
        raise typer.Exit(1)


@app.command()
def resolve(week: WeekOption = None) -> None:
    """Resolve the watchlist against the Torvik player season file; print every entry and its
    alerts, marking with `*` the ones this run raised."""
    wl = _load_watchlist()
    settings = _bootstrap()
    monday = _monday(week)
    engine = db.open_database(settings.data_dir)
    try:
        report = _resolve_week(engine, wl, monday)
    finally:
        engine.dispose()

    typer.echo(f"week of {monday.isoformat()}, run {run_id_for(monday, wl.season)}")
    for line in _resolution_table(report.entries):
        typer.echo(line)
    _echo_new_alerts(report)


def _echo_new_alerts(report: ResolveReport) -> None:
    typer.echo(f"new alerts: {len(report.alerts)}")
    for alert in report.alerts:
        typer.echo(f"  {alert}")


def _resolution_table(entries: list[Entry]) -> list[str]:
    rows = [("NAME", "TEAM", "TIER", "OUTCOME", "MATCH", "ALERT")]
    for entry in entries:
        resolution = entry.resolution
        match = "—"
        if resolution.torvik_pid is not None:
            match = f"{resolution.matched_name} ({resolution.team_slug}, {resolution.torvik_pid})"
        alert = ""
        if entry.alert is not None:
            alert = f"{'*' if entry.new else ' '} {entry.alert.kind} ({entry.alert.detail})"
        rows.append(
            (
                entry.player.name,
                entry.player.team or "—",
                str(entry.player.tier),
                resolution.outcome,
                match,
                alert,
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, [*widths, 0], strict=True)).rstrip()
        for row in rows
    ]


@app.command()
def compute(week: WeekOption = None) -> None:
    """Build the digest from the database and write it to out/digest.json.

    A source missing from the cache or failing validation is treated as failed for this run, as
    `load` treats it: its banner line is printed and its sections are omitted (spec §9.3).
    """
    wl = _load_watchlist()
    settings = _bootstrap()
    monday = _monday(week)
    clients = cache_only_clients()
    try:
        sources = collect(
            clients, _cache(settings), season=wl.season, this_week_start=monday, include_teams=False
        )
    finally:
        clients.close()

    engine = db.open_database(settings.data_dir)
    try:
        digest = _compute_digest(settings, engine, wl, monday, failed_sources(sources))
    finally:
        engine.dispose()

    typer.echo(f"week of {monday.isoformat()}, run {digest.run_id}")
    _echo_digest(digest)
    typer.echo(f"wrote {_out(settings) / DIGEST_JSON}")


def _echo_digest(digest: Digest) -> None:
    for line in digest.banner:
        typer.echo(f"banner: {line}")
    for name, section in (
        ("featured", digest.featured),
        ("watchlist", digest.watchlist),
        ("freshmen", digest.freshmen),
        ("movers", digest.movers),
    ):
        typer.echo(f"{name}: {'omitted' if section is None else f'{len(section.rows)} rows'}")
    typer.echo(f"alerts: {len(digest.alerts)}")


@app.command()
def render(week: WeekOption = None, dry_run: DryRunOption = False) -> None:
    """Render out/digest.json, which `compute` wrote, to out/digest.html and out/digest.txt.

    `render` never sends, so `--dry-run` changes nothing here; it is accepted so the step reads
    the same alone as inside `run --dry-run`. Exits 1 when the digest is missing, unreadable, or
    for another week than `--week`, or when its MJML does not compile.
    """
    del dry_run
    wl = _load_watchlist()
    settings = _bootstrap()
    rendered = _render_digest(settings, _read_digest(settings, wl, week))
    typer.echo(f"subject: {rendered.subject}")


@app.command()
def send(week: WeekOption = None, force: ForceOption = False) -> None:
    """Send out/digest.json, which `compute` wrote, through the transport chain.

    Refuses, exiting 1, when the week's run has already gone out by email, unless `--force`.
    `DRY_RUN=true` writes it to the outbox under `DATA_DIR` instead. Exits 1 when the digest is
    missing or for another week, or when no transport delivers it; 2 when none is configured.
    """
    wl = _load_watchlist()
    settings = _bootstrap()
    digest = _read_digest(settings, wl, week)
    transport = _transport(settings, digest.run_id)
    engine = db.open_database(settings.data_dir)
    try:
        with engine.connect() as connection:
            known = has_run(connection, digest.run_id)
        if not known:
            # Checked before sending: an email whose run cannot record it would be unguarded.
            typer.echo(f"no run {digest.run_id}: run `cbb-digest compute` for that week", err=True)
            raise typer.Exit(1)
        _refuse_if_sent(engine, settings, digest.run_id, force=force)
        rendered = _render_digest(settings, digest)
        _send_digest(settings, engine, transport, digest.run_id, rendered)
    finally:
        engine.dispose()


@dataclass(slots=True)
class _RunRecord:
    """What a run has learned so far, for its `runs` row however it ends."""

    row_counts: dict[str, int] = field(default_factory=dict[str, int])
    failed_sources: list[str] = field(default_factory=list[str])
    alerts: list[str] = field(default_factory=list[str])


@app.command()
def run(
    week: WeekOption = None,
    force: ForceOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Fetch, load, resolve, compute, render and send one week, recording it in `runs`.

    An out-of-season week stops after `resolve` as `skipped_offseason`, sending nothing. A week
    that has already gone out by email is refused, exiting 1, unless `--force`. `--dry-run` (or
    `DRY_RUN=true`) sends to the outbox under `DATA_DIR` instead of by email; it is never refused,
    and never replaces the record of a real send.
    """
    wl = _load_watchlist()
    settings = _bootstrap()
    if dry_run:
        settings = settings.model_copy(update={"dry_run": True})
    monday = _monday(week)
    run_id = run_id_for(monday, wl.season)
    transport = _transport(settings, run_id)
    engine = db.open_database(settings.data_dir)
    try:
        _refuse_if_sent(engine, settings, run_id, force=force)
        with engine.begin() as connection:
            start_run(
                connection,
                run_id=run_id,
                kind="digest",
                season=wl.season,
                windows=windows_for(monday),
                image_sha=settings.image_sha,
                now=SystemClock().now(),
            )
        typer.echo(f"week of {monday.isoformat()}, run {run_id}")
        record = _RunRecord()
        try:
            status = _run_week(settings, engine, wl, monday, transport, record)
        except Exception:
            # §9.3: anything that stops the run records it as failed. `sent_at` survives, so a
            # crash after the email went out still refuses a second send.
            with engine.begin() as connection:
                finish_run(
                    connection,
                    run_id=run_id,
                    status="failed",
                    row_counts=record.row_counts,
                    failed_sources=record.failed_sources,
                    alerts=record.alerts,
                    transport=None,
                    now=SystemClock().now(),
                )
            log.exception("run failed", extra={"run_id": run_id})
            raise
    finally:
        engine.dispose()
    typer.echo(f"status: {status}")


def _run_week(
    settings: Settings,
    engine: Engine,
    wl: Watchlist,
    monday: date,
    transport: Transport,
    record: _RunRecord,
) -> FinalStatus:
    """The steps of `run` after `start_run`, finishing the run's row; the status it ended with.

    A failed source does not stop the run: its sections are omitted and the run ends
    `sent_degraded` (§9.3). With Torvik failed, resolution is skipped and the stored
    resolutions stand.
    """
    run_id = run_id_for(monday, wl.season)
    sources, requests = _fetch_sources(settings, engine, wl.season, monday, refresh_teams=False)
    _echo_fetched(sources, requests)
    failed = failed_sources(sources)
    record.failed_sources = sorted(failed)
    record.row_counts = {
        label: len(result)
        for label, result in sources.outcomes()
        if not isinstance(result, SourceFailure)
    }

    _echo_loaded(_load_week(settings, engine, wl.season, monday, sources))

    if "torvik" in failed:
        typer.echo("resolve: skipped (Torvik unavailable; the stored resolutions stand)")
    else:
        _echo_new_alerts(_resolve_week(engine, wl, monday))

    with engine.begin() as connection:
        skipped = finish_if_offseason(
            connection,
            run_id=run_id,
            monday=monday,
            game_logs_fetched=not isinstance(sources.player_games, SourceFailure),
            espn_fetched=not isinstance(sources.espn_week, SourceFailure),
            row_counts=record.row_counts,
            failed_sources=record.failed_sources,
            now=SystemClock().now(),
        )
    if skipped:
        typer.echo("out of season: nothing to send")
        return "skipped_offseason"

    digest = _compute_digest(settings, engine, wl, monday, failed)
    record.alerts = [str(alert) for alert in digest.alerts]
    _echo_digest(digest)

    rendered = _render_digest(settings, digest)
    receipt = _send_digest(settings, engine, transport, run_id, rendered)

    status: FinalStatus = "sent_degraded" if failed else "sent"
    with engine.begin() as connection:
        finish_run(
            connection,
            run_id=run_id,
            status=status,
            row_counts=record.row_counts,
            failed_sources=record.failed_sources,
            alerts=record.alerts,
            transport=receipt.transport,
            now=SystemClock().now(),
        )
    return status


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
