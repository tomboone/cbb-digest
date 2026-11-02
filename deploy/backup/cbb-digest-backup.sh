#!/bin/sh
# Nightly copy of the cbb-digest SQLite database, kept on the USB drive and synced to B2.
# Run by cbb-digest-backup.timer as tom. Install and operate per ../README.md, "Nightly backup to B2".
set -eu

ENV_FILE=${ENV_FILE:-$HOME/.config/cbb-digest-backup.env}
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 2; }
# shellcheck source=/dev/null
. "$ENV_FILE"
: "${MOUNT_POINT:?}" "${BACKUP_DIR:?}" "${REMOTE:?}"

# Reports to a Kuma push monitor (the Kuma on kumadrome): status=up after a good run, status=down with
# the reason on failure. A run that never happens is caught by the monitor's heartbeat interval.
ping() {  # $1=status (up|down) $2=msg
  [ -n "${PING_URL:-}" ] || return 0
  # Tolerate a URL pasted with Kuma's default "?status=up&msg=OK&ping=" query.
  curl -fsS -m 15 --retry 3 -o /dev/null --get "${PING_URL%%\?*}" \
    --data-urlencode "status=$1" --data-urlencode "msg=$2" || echo "ping failed" >&2
}
fail() { echo "$1" >&2; ping down "$1"; exit 1; }

# A dropped drive leaves /mnt/music as an empty directory on the root disk: a copy written there is
# lost with the next mount, and syncing an empty BACKUP_DIR would hide every copy on B2.
mountpoint -q "$MOUNT_POINT" || fail "$MOUNT_POINT is not mounted"

# The database of the compose project this script was checked out with (~/cbb-digest/deploy).
DB=$(dirname "$0")/../data/cbb-digest.sqlite3
[ -r "$DB" ] || fail "cannot read $DB"

# `.backup` uses SQLite's online backup API, so the copy is consistent while the container writes;
# a `cp` of a live WAL database is not. Opened read-only: the data directory belongs to the
# container's UID 999, so tom can read the database but not create its -wal and -shm files. Those
# exist while the container holds the database open, which `serve` does for its whole life (its
# engine's connection pool, disposed only on exit); with the container stopped they are gone and the
# open fails, as closinglineup's pg_dump does with Postgres down.
# Written under a dot-name (which rclone below skips) and renamed once checked, so a failed run never
# leaves a file that looks like a good copy.
mkdir -p "$BACKUP_DIR" || fail "cannot create $BACKUP_DIR"
ts=$(date +%Y%m%d-%H%M%S)
tmp="$BACKUP_DIR/.cbb-digest-$ts.sqlite3"
sqlite3 -bail -readonly "$DB" ".backup '$tmp'" \
  || { rm -f "$tmp"; fail "sqlite3 .backup failed (is the cbb-digest container running?)"; }
check=$(sqlite3 -bail "$tmp" 'PRAGMA integrity_check') || check="integrity_check did not run"
[ "$check" = ok ] || { rm -f "$tmp" "$tmp-wal" "$tmp-shm"; fail "copy failed integrity_check: $check"; }
mv "$tmp" "$BACKUP_DIR/cbb-digest-$ts.sqlite3" || fail "copy rename failed"
find "$BACKUP_DIR" -name 'cbb-digest-*.sqlite3' -mtime +"${KEEP_DAYS:-14}" -delete \
  || fail "pruning old copies failed"

# B2 hides rather than deletes (the remote's hard_delete = false), so a copy aged out here stays
# recoverable per the bucket's lifecycle.
rclone sync "$BACKUP_DIR" "$REMOTE" --exclude '.*' --fast-list --log-level NOTICE \
  || fail "B2 sync failed"

ping up OK
