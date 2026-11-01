"""The `runs` row: one per attempt at a run id, from `running` to how it ended (spec §9.4).

`start_run` opens the row, and `finish_run` closes it. Restarting a run id resets its row to
`running` rather than adding another, and clears everything the last attempt finished with. The
exception is `sent_at` and `transport`: they say the email went out, and only a later send moves
them. So a restarted week that already went out still refuses to send again without `--force`
(spec §9.2).

`ensure_run` is the third writer: `load` and `resolve`, run on their own, need a row for their
rows to reference, and it inserts a placeholder without touching one that exists.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cbb_digest.schema import runs
from cbb_digest.weeks import Windows

type RunKind = Literal["digest", "reset"]
type FinalStatus = Literal["sent", "sent_degraded", "skipped_offseason", "skipped_missed", "failed"]

SENT_STATUSES: frozenset[FinalStatus] = frozenset({"sent", "sent_degraded"})


def start_run(
    connection: Connection,
    *,
    run_id: str,
    kind: RunKind,
    season: int,
    windows: Windows | None,
    image_sha: str | None,
    now: datetime,
) -> None:
    """Open `run_id` as `running`, or reset it to `running` if an earlier attempt left a row.

    `windows` is `None` for a reset run, which has none (spec §11).
    """
    values = {
        "kind": kind,
        "season": season,
        **_window_values(windows),
        "status": "running",
        "started_at": now,
        "finished_at": None,
        "duration_s": None,
        "row_counts": None,
        "failed_sources": None,
        "alerts": None,
        "image_sha": image_sha,
    }
    connection.execute(
        sqlite_insert(runs)
        .values(run_id=run_id, **values)
        .on_conflict_do_update(index_elements=[runs.c.run_id], set_=values)
    )


def finish_run(
    connection: Connection,
    *,
    run_id: str,
    status: FinalStatus,
    row_counts: Mapping[str, int],
    failed_sources: Sequence[str],
    alerts: Sequence[str],
    transport: str | None,
    now: datetime,
) -> None:
    """Close `run_id` with `status`, stamping `finished_at` and the duration since it started.

    `transport` names the transport that sent the email, and is given exactly when `status` is
    `sent` or `sent_degraded`; `sent_at` is then `now`. A run that sent nothing keeps whatever
    `sent_at` and `transport` an earlier attempt stored.
    """
    if (status in SENT_STATUSES) != (transport is not None):
        raise ValueError(f"status {status!r} with transport {transport!r}")
    started_at = connection.execute(
        select(runs.c.started_at).where(runs.c.run_id == run_id)
    ).scalar_one_or_none()
    if started_at is None:
        raise LookupError(f"no run {run_id!r} to finish")
    values: dict[str, object] = {
        "status": status,
        "finished_at": now,
        "duration_s": (now - started_at).total_seconds(),
        "row_counts": dict(row_counts),
        "failed_sources": list(failed_sources),
        "alerts": list(alerts),
    }
    if transport is not None:
        values |= {"sent_at": now, "transport": transport}
    connection.execute(update(runs).where(runs.c.run_id == run_id).values(values))


def ensure_run(
    connection: Connection, *, run_id: str, season: int, windows: Windows, now: datetime
) -> None:
    """Insert a placeholder `runs` row (`status=running`) for `run_id` if it has none, so rows
    that reference the run can be written. An existing row is never changed."""
    connection.execute(
        sqlite_insert(runs)
        .values(
            run_id=run_id,
            kind="digest",
            season=season,
            **_window_values(windows),
            status="running",
            started_at=now,
        )
        .on_conflict_do_nothing(index_elements=[runs.c.run_id])
    )


def _window_values(windows: Windows | None) -> dict[str, datetime | None]:
    return {
        "last_week_start": windows.last_week.start if windows else None,
        "last_week_end": windows.last_week.end if windows else None,
        "this_week_start": windows.this_week.start if windows else None,
        "this_week_end": windows.this_week.end if windows else None,
    }
