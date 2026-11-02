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

Commands below run on kumadrome, in a shell opened with `ssh kumadrome`. The one that needs `sudo`
prompts for your password there.

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
