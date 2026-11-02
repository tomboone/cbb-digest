# Deploying cbb-digest on kumadrome

cbb-digest runs as one container on kumadrome, with its own Watchtower beside it pulling new images
from GHCR. kumadrome is the home Mac mini (Ubuntu, Docker with the Compose plugin), and nba-today,
mlb-today and closinglineup run on the same host. The container runs `cbb-digest serve`: a loop that
sleeps until Monday 07:00 America/New_York and keeps a heartbeat in SQLite for its health check.

Images are published by `.github/workflows/image.yml` on every push to `main`:

| Tag | Use |
|---|---|
| `latest` | what kumadrome runs |
| `sha-<7>` | pinning or rolling back to one commit |

Commands below run on kumadrome, in a shell opened with `ssh kumadrome`. Those that need `sudo`
prompt for your password there.

## 1. Make sure kumadrome can pull the image

Watchtower pulls anonymously, so the `ghcr.io/tomboone/cbb-digest` package must be public: your
GitHub profile → Packages → `cbb-digest` → Package settings → Change visibility → Public. Check it
from kumadrome:

```sh
docker pull ghcr.io/tomboone/cbb-digest:latest
```

It ends with `Status: Downloaded newer image for ghcr.io/tomboone/cbb-digest:latest` (or
`Image is up to date`). `denied` or `unauthorized` means the package is still private.

## 2. Fetch the deploy files

Only `deploy/` is needed. A sparse clone fetches that directory, plus the top-level files git always
includes (among them `.env.example`, used in step 4), without the source tree:

```sh
git clone --filter=blob:none --sparse --branch main https://github.com/tomboone/cbb-digest.git ~/cbb-digest
cd ~/cbb-digest
git sparse-checkout set deploy
cd deploy
```

Every command from here on runs in `~/cbb-digest/deploy/`. `git pull` picks up later changes to
these files.

## 3. Make the data directory

The container keeps the source cache, the SQLite database (`cbb-digest.sqlite3`) and dry-run output
in `/data`, which is `./data` on the host. It runs as a non-root user with UID/GID 999, so the
directory must be owned by it, or Docker creates it root-owned and the app cannot write:

```sh
mkdir -p data
sudo chown 999:999 data
```

`ls -ldn data` shows `999 999` as its owner and group.

## 4. Configure `.env`

Copy the template, then set the CollegeBasketballData API key and turn on dry-run mode. The key is
read without echoing it, so it never lands in your shell history; paste it at the `CBBD API key:`
prompt and press Enter:

```sh
cp ../.env.example .env
chmod 600 .env
printf 'CBBD API key: ' && read -rs CBBD_API_KEY && echo
sed -i '/^CBBD_API_KEY=/d' .env && printf 'CBBD_API_KEY=%s\n' "$CBBD_API_KEY" >>.env
unset CBBD_API_KEY
sed -i '/^#\? *DRY_RUN=/d' .env && echo 'DRY_RUN=true' >>.env
```

Check both took:

```sh
grep -c '^CBBD_API_KEY=..*' .env   # 1
grep '^DRY_RUN=' .env              # DRY_RUN=true
```

`RECIPIENTS` already defaults to `tom@tomboone.com`. Every other variable stays blank for now: the
Resend credentials are set for the first real send (step 6), and the healthchecks.io URL when the
digest goes live. Every variable and its default is documented in `.env.example`.

With `DRY_RUN=true`, a run renders the digest to `data/out/` and writes it to
`data/outbox/<run id>.html` instead of emailing it. A dry run never blocks the real send of the
same week. For now `serve` only keeps its heartbeat and logs the next send time.

## 5. Start it

```sh
docker compose config -q   # validates the compose file and .env; prints nothing on success
docker compose up -d
docker compose ps
```

Within a minute `docker compose ps` shows both services `healthy` (each image carries its own
health check). `serve` migrates the database and writes its first heartbeat as soon as it starts,
and Docker probes every 5 seconds during the start period, so `cbb-digest` turns `healthy` within
seconds; after that it is checked every 5 minutes.

The log shows the next send time:

```sh
docker compose logs cbb-digest | grep '"next send"' | tail -1
```

prints a line whose `send_at` is the coming Monday at `07:00:00-05:00` (`-04:00` while daylight
saving time is in effect).

## 6. First real send

This sends one real digest for the current week to `RECIPIENTS`, by hand, and checks it arrives and
reads well. It leaves `DRY_RUN=true` in `.env`, so nothing is sent on a schedule until the digest
goes live.

The run is for the Monday of the week containing today, in Eastern time, and that Monday must be
in season: a Division I game last week or this week. Out of season the run prints
`out of season: nothing to send`, ends with `status: skipped_offseason` and sends nothing; run it
again once the season has started.

**1. Set the Resend credentials.** The API key is the one nba-today already uses; it is in
nba-today's `.env`. `tomboone.com` is already a verified domain in Resend, for mlb-today and
nba-today, so `cbb@tomboone.com` can send without any DNS change. Paste the key at the
`Resend API key:` prompt and press Enter:

```sh
printf 'Resend API key: ' && read -rs RESEND_API_KEY && echo
sed -i '/^RESEND_API_KEY=/d' .env && printf 'RESEND_API_KEY=%s\n' "$RESEND_API_KEY" >>.env
unset RESEND_API_KEY
sed -i '/^RESEND_FROM=/d' .env && echo 'RESEND_FROM=CBB Digest <cbb@tomboone.com>' >>.env
sed -i '/^RECIPIENTS=/d' .env && echo 'RECIPIENTS=tom@tomboone.com' >>.env
sed -i '/^#\? *DRY_RUN=/d' .env && echo 'DRY_RUN=true' >>.env
```

Check all four took:

```sh
grep -c '^RESEND_API_KEY=re_..*' .env   # 1
grep '^RESEND_FROM=' .env               # RESEND_FROM=CBB Digest <cbb@tomboone.com>
grep '^RECIPIENTS=' .env                # RECIPIENTS=tom@tomboone.com
grep '^DRY_RUN=' .env                   # DRY_RUN=true
```

**2. Restart** so the container picks up the new `.env`:

```sh
docker compose up -d
docker compose ps
```

`cbb-digest` shows `healthy` again within a minute.

**3. Dry run.** It fetches every source, builds the digest and writes it to `data/out/` and
`data/outbox/`, emailing nobody:

```sh
docker compose exec cbb-digest cbb-digest run --dry-run
```

It prints `week of <Monday>, run <run id>`, for example `run 2027-W45`, and ends with
`status: sent`. `status: sent_degraded` means a source failed: the top of the digest names it, and
its sections are missing. Don't go on to step 4 then; run the dry run again later, until it ends
with `status: sent`.

Copy the digest to your Mac and open it in a browser. The file is readable only by the container's
user, so it is read through the container. Run this on the Mac, not on kumadrome:

```sh
ssh kumadrome 'cd ~/cbb-digest/deploy && docker compose exec -T cbb-digest cat /data/out/digest.html' >~/Downloads/cbb-digest.html
open ~/Downloads/cbb-digest.html
```

Check every section is there and its links open Torvik pages. A dry run does not count as a send
(spec §9.2), so it does not block step 4.

**4. Real send.** Back on kumadrome, run the same week with `DRY_RUN` overridden for this one
command; `.env` keeps `DRY_RUN=true`:

```sh
docker compose exec -e DRY_RUN=false cbb-digest cbb-digest run
```

It prints the same `week of <Monday>, run <run id>` line as step 3 and ends with `status: sent`.
The command deliberately leaves out `--force`, so running it a second time for the same week exits 1
with `run <run id> has already been sent; pass --force to send it again` rather than emailing the
week again.

If a source failed between steps 3 and 4, the run ends with `status: sent_degraded`: the email still
went out, with the failed source named at its top. Check it in step 6 anyway. Once a dry run ends
with `status: sent` again, send the full digest for the week a second time:

```sh
docker compose exec -e DRY_RUN=false cbb-digest cbb-digest run --force
```

**5. Check the run was recorded:**

```sh
docker compose exec cbb-digest python -c 'import sqlite3; print(sqlite3.connect("/data/cbb-digest.sqlite3").execute("select run_id, status, transport, sent_at from runs order by started_at desc limit 1").fetchone())'
```

It prints the run id from step 4, then `'sent'`, `'resend'` and the time the email went out, in
UTC.

**6. Check the email.** It arrives at `tom@tomboone.com` from `CBB Digest <cbb@tomboone.com>`, with
the subject `CBB digest — week of <Mon D>`, for example `CBB digest — week of Nov 2`. Check that:

- it is in the inbox, not in spam;
- it reads correctly in Fastmail on the web: sections in order, tables not overflowing, links
  opening Torvik pages;
- it reads correctly in the Fastmail app on your phone, with no sideways scrolling.

File each rendering problem as a bug ticket against the templates. Turning on the weekly send is the
go-live step, not this one.

## 7. Nightly backup to B2

The database holds the history no source can give back: the weekly ratings snapshots behind movers,
and the resolution rows behind alert-on-change. `backup/cbb-digest-backup.sh` copies it nightly at
05:30 Eastern (+ up to 15 min) from a systemd timer on the host, as `tom`. It:

- refuses to run unless `/mnt/music` is mounted;
- copies `data/cbb-digest.sqlite3` with the host's `sqlite3 .backup` (SQLite's online backup, safe
  while the container writes; a `cp` of a live WAL database is not) into a dated
  `cbb-digest-<YYYYmmdd-HHMMSS>.sqlite3` in `/mnt/music/cbb-digest/backups`, checks it with
  `PRAGMA integrity_check`, and deletes copies older than `KEEP_DAYS` (14);
- `rclone sync`s that directory to `cbb-digest-backups/` in the B2 bucket the Navidrome backup uses.
  The `b2` remote has `hard_delete = false`, so a copy aged out stays recoverable per the bucket's
  lifecycle;
- pings a push monitor on the Kuma on kumadrome: `up` after a good run, `down` with the reason on
  failure.

It reads the database read-only, because `data/` belongs to the container's UID 999. That works
while the container is running, which holds the database open; with `cbb-digest` stopped, the
night's run fails with `sqlite3 .backup failed (is the cbb-digest container running?)`.

| File | Purpose |
|---|---|
| `backup/cbb-digest-backup.sh` | The job. POSIX `sh`. Reads `~/.config/cbb-digest-backup.env`. |
| `backup/cbb-digest-backup.env.example` | Its settings: the paths, `KEEP_DAYS`, the B2 destination, and `PING_URL`. |
| `backup/cbb-digest-backup.service`, `.timer` | Oneshot service as `tom`, with `RequiresMountsFor=/mnt/music`. `Persistent` catches up after downtime. |

### Install

rclone and the `b2` remote are already on kumadrome, from the Navidrome backup (`rclone listremotes`
lists `b2:`).

**1. `sqlite3` on the host.** `sqlite3 --version` prints a version: 3.22 or later, the first that
reads a WAL database it cannot write to (kumadrome has 3.46). If it says `command not found`, install
it:

```sh
sudo apt-get install -y sqlite3
```

**2. The copy directory**, on the drive. `/mnt/music` belongs to `tom`, so no `sudo`:

```sh
mountpoint /mnt/music && mkdir -p /mnt/music/cbb-digest/backups
```

The first command prints `/mnt/music is a mountpoint`; if it doesn't, the drive is not mounted, and
the directory is not created.

**3. A push monitor** on the Kuma on kumadrome (`http://127.0.0.1:3001`): *Add New Monitor*, type
*Push*, name `kumadrome/cbb-digest-backup`, heartbeat interval 25 h (90000 s), retries 0. Copy its
push URL.

**4. Settings.** Copy the template, set the B2 destination from the Navidrome backup's bucket (its
`DB_REMOTE`), and
set the push URL from step 3. Paste the URL at the `Kuma push URL:` prompt and press Enter. It is
written in double quotes: the file is sourced, and an unquoted `&` from Kuma's default
`?status=up&msg=OK&ping=` query would leave `PING_URL` unset and skip the ping silently.

```sh
cp backup/cbb-digest-backup.env.example ~/.config/cbb-digest-backup.env
chmod 600 ~/.config/cbb-digest-backup.env
bucket=$(sed -n 's#^DB_REMOTE=b2:\([^/]*\)/.*#\1#p' ~/.config/navidrome-backup.env)
[ -n "$bucket" ] && sed -i "s#^REMOTE=.*#REMOTE=b2:$bucket/cbb-digest-backups#" ~/.config/cbb-digest-backup.env \
  || echo 'no bucket: DB_REMOTE=b2:<bucket>/… not found in ~/.config/navidrome-backup.env'
printf 'Kuma push URL: ' && read -r PING_URL
sed -i '/^PING_URL=/d' ~/.config/cbb-digest-backup.env && printf 'PING_URL="%s"\n' "$PING_URL" >>~/.config/cbb-digest-backup.env
unset bucket PING_URL
```

Check every value took:

```sh
grep -E '^[A-Z_]+=' ~/.config/cbb-digest-backup.env
```

It prints `MOUNT_POINT=/mnt/music`, `BACKUP_DIR=/mnt/music/cbb-digest/backups`, `KEEP_DAYS=14`,
`REMOTE=b2:<the bucket>/cbb-digest-backups` with a real bucket name after `b2:`, and
`PING_URL="http://127.0.0.1:3001/api/push/…"`. No line still contains `<bucket>` or `<token>`.

**5. Timer**, and a first run in the foreground. Run it with `cbb-digest` up (`docker compose ps`
shows it `healthy`):

```sh
sudo cp backup/cbb-digest-backup.service backup/cbb-digest-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cbb-digest-backup.timer
sudo systemctl start cbb-digest-backup.service
systemctl status cbb-digest-backup.service --no-pager; journalctl -u cbb-digest-backup --since -1h
systemctl list-timers cbb-digest-backup.timer
ls -l /mnt/music/cbb-digest/backups
. ~/.config/cbb-digest-backup.env && rclone ls "$REMOTE"
```

`status` shows the service `inactive (dead)` with `status=0/SUCCESS`, `list-timers` shows the next
run after 05:30, the copy is in both listings, and `kumadrome/cbb-digest-backup` shows an `up`
heartbeat.

The units run the script from this clone, so a `git pull` updates the job. After a change to a unit
file, copy it again and `sudo systemctl daemon-reload`.

### Restore

Restoring is by hand.

**1. Pick the copy.** This takes the newest on B2, which holds every copy the drive does, and fetches
it to the drive if the drive's copy is gone:

```sh
. ~/.config/cbb-digest-backup.env
ts=$(rclone lsf "$REMOTE" --include 'cbb-digest-*.sqlite3' | sed 's/^cbb-digest-//; s/\.sqlite3$//' | sort | tail -1)
copy="$BACKUP_DIR/cbb-digest-$ts.sqlite3"
[ -r "$copy" ] || rclone copy "$REMOTE/cbb-digest-$ts.sqlite3" "$BACKUP_DIR/"
ls -l "$copy"
```

`ls` lists one file, named for the newest copy. To restore an older one, `rclone lsf "$REMOTE"` lists
them all; set `ts` to the one you want, for example `ts=20261102-053712`, then run the `copy=` line
and the two after it again. If the drive itself is gone, run `BACKUP_DIR=/tmp` before the `copy=` line.

**2. Drill** on a scratch copy, which leaves the live database alone. It prints `ok`, the last three
runs as of the copy, and the Alembic revision:

```sh
cp "$copy" /tmp/restore-drill.sqlite3
sqlite3 /tmp/restore-drill.sqlite3 'PRAGMA integrity_check' \
  'select run_id, status, sent_at from runs order by started_at desc limit 3' \
  'select version_num from alembic_version'
rm -f /tmp/restore-drill.sqlite3*
```

**3. For real**, stop the container, put the copy in place owned by UID 999, delete the old database's
`-wal` and `-shm` (left beside the copy, SQLite would replay them into it), and start it again.
`serve` migrates on start, so a copy from an older revision is brought up to head:

```sh
docker compose stop cbb-digest
sudo install -o 999 -g 999 -m 644 "$copy" data/cbb-digest.sqlite3
sudo rm -f data/cbb-digest.sqlite3-wal data/cbb-digest.sqlite3-shm
docker compose start cbb-digest
docker compose ps
```

`cbb-digest` shows `healthy` within a minute.

A copy taken before this week's send has no `sent_at` for it, so `serve` sends the week again if it
starts between Monday 07:00 and Tuesday noon: the drill's `runs` shows whether the copy has this
week's run as `sent`. If it does not, and you don't want the week sent twice, leave out the
`docker compose start` line and run it after Tuesday 12:00 instead; `serve` then records the week
`skipped_missed` and sends nothing.

## Day-to-day

**Logs.** The app logs structured JSON to stdout; follow it with
`docker compose logs -f cbb-digest`. Image updates show in `docker compose logs -f watchtower`.

**Health:** `docker compose exec cbb-digest cbb-digest health; echo $?` exits 0 while `serve` has
ticked within the last 8 days. It ticks at least hourly.

**Updates** are automatic: Watchtower checks GHCR every 5 minutes and restarts `cbb-digest` on the new
image, removing the old one. To confirm a merge to `main` has landed:

```sh
docker inspect --format '{{.Image}} {{.Created}}' "$(docker compose ps -q cbb-digest)"
docker compose exec cbb-digest printenv IMAGE_SHA
```

The first line is the image ID and the time the container was created, both new after Watchtower
recreates it; `IMAGE_SHA` is the full SHA of the commit the image was built from, which matches the
merge commit on `main` (`git log -1 --format=%H origin/main` after a `git pull`).

To update by hand: `docker compose pull && docker compose up -d`.

The Watchtower runs with `--scope cbb-digest`, so it only touches containers labelled with that scope.
The other stacks on kumadrome run their own scoped Watchtowers. Each instance updates only the enabled
containers in its own scope, so no two instances may share one
([Watchtower docs](https://watchtower.nickfedor.com/advanced-features/running-multiple-instances/)).

**Roll back** by pinning a commit: change the `image` tag in `docker-compose.yml` to `sha-<7>` and
run `docker compose up -d`. Watchtower keeps following that tag, which never moves, so updates stop
until you switch back. The pin is a local edit to a tracked file, so drop it with
`git restore docker-compose.yml` before the next `git pull`.
