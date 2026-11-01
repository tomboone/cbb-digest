"""Command-line entry point (spec §9.1). Every command is a stub until its ticket lands."""

from datetime import datetime
from typing import Annotated, NoReturn

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)

# Plain aliases, not `type`: Typer cannot see through a TypeAliasType.
DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Write out/digest.html and out/digest.txt; send nothing.")
]
ForceOption = Annotated[
    bool, typer.Option("--force", help="Send even if this run id has already been sent.")
]


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
    _not_implemented("run")


@app.command()
def serve() -> None:
    """Run the weekly scheduler loop (container entrypoint)."""
    _not_implemented("serve")


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
    _not_implemented("health")
