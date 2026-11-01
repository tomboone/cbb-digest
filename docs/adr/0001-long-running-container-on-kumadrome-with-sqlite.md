# One long-running container on kumadrome, state in SQLite

The handoff placed cbb-digest on the Hetzner Coolify box as a scheduled task with its own Postgres resource, chosen to fit the VM's Postgres backup script. cbb-digest instead runs the way its siblings nba-today and mlb-today do: one container on kumadrome, published to GHCR on every push to `main` and deployed by a scoped Watchtower. The container is long-running (`cbb-digest serve`) and sleeps until Monday 07:00 America/New_York, as nba-today's ADR-0001 does. Watchtower with `--label-enable` only updates running containers, so a host timer firing a one-shot container would need to pull its own image. State lives in one SQLite file on the data volume (SQLAlchemy core + Alembic). The data is small, and a Postgres container would be a second service with a second backup to maintain.

## Consequences

- The process must be restart-safe. On boot it reads the `runs` table for the current run id (season + ISO week), so a restart never sends twice or skips a week.
- Backups copy closinglineup's kumadrome pattern: a host systemd timer runs `sqlite3 .backup` plus `PRAGMA integrity_check` to the USB drive, `rclone sync`s to B2, and pushes to a Kuma monitor.
- Alembic on SQLite needs batch mode, and autogenerate can emit a stray `drop_table` for `_alembic_tmp_*` tables. Every migration is reviewed by hand.
