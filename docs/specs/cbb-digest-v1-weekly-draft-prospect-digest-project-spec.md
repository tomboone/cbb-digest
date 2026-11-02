# cbb-digest v1: weekly draft-prospect digest — project spec

Linear initiative: cbb-digest. Project: "cbb-digest v1: weekly draft-prospect digest" (team NEU). Glossary: `CONTEXT.md`. Decisions: `docs/adr/`. Original brief: `docs/CBB Weekly Digest — Handoff for Claude Code.md`. **Where this spec and the brief disagree, this spec wins.** It records the outcome of the 2026-10-31 shaping session.

This spec is the shared reference for every ticket in the project. A ticket needs its own `/planit` spec only where this document says nothing about it.

Reference implementation: **nba-today** (`~/projects/nba-today`, tomboone/nba-today). Where this spec says **"port from nba-today"**, copy the module, rename `nba_today` to `cbb_digest`, and change only what this spec calls for. Do not extract a shared package.

## 1. Purpose and success test

Tom follows the NBA, not college basketball. cbb-digest emails him one Monday-morning **digest** per week **in season**. It tells him (a) what his **watchlist** of 25–40 college draft prospects did **last week**, (b) which games **this week** are worth watching, (c) which teams moved in the top 25, and (d) which freshmen not on his list are playing big minutes. It merges sources that existing sites cover separately, around a personal list.

**Success test:** by February 2027 Tom is still opening the email and picks at least one game a week from it. If not, the project is shelved, not expanded.

## 2. Scope

**In v1:** the weekly digest (§5), watchlist resolution (§7), unattended weekly operation on kumadrome (§10), and the on-demand **portal-reset digest** (§11, final milestone, built in March 2027).

**Not in v1:** a web UI or dashboard; news, rumours, recruiting, NIL or coaching changes; women's, non-D-I or international players; predictions, betting or fantasy content; real-time or per-game alerts; per-game box-score emails; historical backfill before the current season; career highs; injury data; shared code with closinglineup or nba-today.

**Constraints:** free data only, with no KenPom, no paid APIs and nothing behind a login. **No bot-check circumvention, ever** (ADR-0002). Fewer than 20 external requests per weekly run. One recipient, with no auth, no unsubscribe and no multi-tenancy. No new SaaS beyond a CBBD key and a healthchecks.io check.

## 3. Decisions (summary; ADRs hold the rationale)

| Area | Decision |
|---|---|
| Host | kumadrome. One long-running container plus a Watchtower scoped to `cbb-digest`. GHCR image `ghcr.io/tomboone/cbb-digest` (ADR-0001). |
| Scheduler | In-app: `cbb-digest serve` sleeps until Monday 07:00 `America/New_York`. Restart-safe through the `runs` table (ADR-0001). |
| Storage | SQLite file `/data/cbb-digest.sqlite3`, SQLAlchemy core + Alembic in batch mode (ADR-0001). |
| Backup | Host systemd timer, nightly: `sqlite3 .backup` → `PRAGMA integrity_check` → USB drive → `rclone sync` to B2 → Kuma push. Cloned from closinglineup's `deploy/backup/`. |
| Team ratings | CBBD adjusted net rating. **Rank** = position 1..N by net rating, highest first; teams with equal net rating keep CBBD's list order (ADR-0002). CBBD's own `rankings.net` gives tied teams the same rank and is not used. |
| Player stats | Torvik player season file (plain GET, CSV) and player game logs (plain GET, gzipped JSON). |
| Schedule and TV | ESPN unofficial scoreboard. |
| Email | Jinja2 → MJML → `mjml-python`, plus a Jinja2 text part. Port nba-today's `render/`. |
| Transport | Resend (primary) from `cbb@tomboone.com` on the domain already verified for mlb-today/nba-today, with an optional Fastmail SMTP fallback and a dry-run outbox. Port nba-today's `transport/`. To: `tom@tomboone.com`. |
| Alerting | healthchecks.io (period 7 d, grace 26 h). Ping every scheduled Monday, whether it sent or skipped, and `/fail` with a reason on failure. JSON logs to stdout. No Bugsink, SigNoz or Sentry. |
| Watchlist | `watchlist.yaml` at the repo root, baked into the image and edited by PR. Each run records the image's git SHA. |
| Repo | `tomboone/cbb-digest`, public, MIT. GHCR package public, so Watchtower can pull anonymously. |
| Toolchain | Python 3.13, uv, ruff (lint + format), pyright (strict), pytest, respx, syrupy, httpx, pydantic v2 + pydantic-settings, typer, rapidfuzz, SQLAlchemy 2 core, Alembic, PyYAML, mjml-python, Jinja2. Same `pyproject.toml` tool config as nba-today. |
| CI and branches | GitHub Actions lints and tests every PR. On push to `main` it builds and pushes `:latest` and `:sha-<7>`. PRs target `main` and merge as merge commits. Standard rulesets, Dependabot to `updates/dependencies`, and the weekly merge workflow (copy nba-today's). |

## 4. Glossary pointers

See `CONTEXT.md` for **watchlist**, **watchlist player**, **tier**, **class**, **season**, **resolution**, **alert**, **inactive**, **digest**, **portal-reset digest**, **reset status**, **featured game**, **notable**, **freshman to watch**, **mover**, **last week**, **this week**, **in season**, **run**, **ratings snapshot**, **rank** and **canonical team**. Code uses those names.

## 5. The digest

Plain responsive HTML (MJML) with a text part. Readable on a phone in under five minutes. No images and no tracking pixels.

**Subject:** `CBB digest — week of Nov 9`, using the run's Monday date in the format `%b %-d`.

**Source-failure banner:** if any source still failed after the retry (§9.3), the top of the email carries one line per failed source, e.g. `ESPN schedule unavailable this week — featured games omitted.` The sections that depend on that source are omitted, and the rest is sent.

**Sections, in order:**

| # | Section | Content | Depends on |
|---|---|---|---|
| 1 | Featured games | Up to **5** featured games this week (§8.3). Each row shows date, tip time ET, network(s), both teams with ranks, and the watchlist players involved, by tier. If more qualify: `+N more games involving your watchlist`. | ESPN, CBBD, resolution |
| 2 | Watchlist last week | One row per resolved watchlist player whose team played last week: team, games, MPG, PTS/REB/AST per game, TS%, 3PA per game, and at most one **notable** (§8.4). Sorted by tier ↑, then minutes ↓. Capped at **40** rows; `+N more` links to the Torvik player page list. Players whose team did not play are left out. | Torvik game logs, resolution |
| 3 | Freshmen to watch | **5** freshmen not on the watchlist (§8.5): team, rank of team, MPG, PTS/REB/AST, TS%. | Torvik season file, CBBD |
| 4 | Top-25 movers | Teams in the CBBD top 25 that moved by 3 or more places, entered or left, with the rank change vs the previous snapshot. Capped at **10**; `+N more` links to Torvik's T-Rank page, labelled "full ratings table (Torvik's; ranks differ slightly)". The first run of a season shows `Baseline snapshot taken — movers start next week.` | CBBD snapshots |
| 5 | Watchlist alerts | Alerts raised this week (§7.4). Omitted when empty. | Resolution |
| 6 | Footer | Run timestamp (ET), data as-of date, run id, image SHA, and a link to `watchlist.yaml` on GitHub at that SHA. | — |

**TS%** = PTS / (2 × (FGA + 0.44 × FTA)) over the games in the window. It shows `—` when FGA + FTA = 0.

**Formatting:** per-game stats to one decimal; percentages as whole numbers (`TS 58%`); times as `7:00 PM ET`; ranks as `#12`. Every team and player name in sections 1–4 links to its Torvik page, with two exceptions. A team the crosswalk does not know, such as a non-D-I opponent, is shown under ESPN's name without a link (§7.5). The watchlist alerts (section 5) name each entry as written in `watchlist.yaml`, without a link: an alert carries no Torvik id or team, and an unresolved or ambiguous entry has no page to link to. Names are URL-encoded (`p=Bennett%20Stirtz`). The link forms, all confirmed by clicking them in a browser on 2026-11-01:

| Link | URL |
|---|---|
| Player | `https://barttorvik.com/playerstat.php?link=y&year=<season>&p=<name>&t=<torvik team>` |
| Team | `https://barttorvik.com/team.php?team=<torvik team>&year=<season>` |
| Watchlist `+N more` (Torvik's player list) | `https://barttorvik.com/playerstat.php?link=y&year=<season>` |
| Movers `+N more` (Torvik's T-Rank page) | `https://barttorvik.com/trank.php?year=<season>` |

These are links for the reader's browser only. Torvik answers a non-browser client on the player page and `trank.php` with its browser check (§6), and the pipeline never fetches any of them. The player and team forms come from a GitHub code search on 2026-11-01. The player form there carries `year=`, which the original form lacked; the team form matched the original. The two `+N more` forms were chosen when the templates were built (NEU-2110).

## 6. Data sources

Probed on 2026-10-31 and again on 2026-11-01 (NEU-2093; 15 requests). All requests used a browser User-Agent. The fixtures, request log and full positional column maps are in `tests/fixtures/` and its `README.md`.

**Torvik** (`https://barttorvik.com`, no key):

| Endpoint | Finding |
|---|---|
| `trank.php?year=2027&json=1` | **Blocked**: HTML "Verifying Browser…" JavaScript challenge. Not used (ADR-0002). |
| `getadvstats.php?year=<season>&csv=1`: **player season file** | 200 `text/csv`, headerless, 67 fields per row, in hoopR's `R/torvik_player_stats.R` order. Shooting makes and attempts (`ftm`, `fta`, `two_pm`, `two_pa`, `three_pm`, `three_pa`) are **season totals**. `minutes`, `oreb`, `dreb`, `treb`, `ast`, `stl`, `blk` and `pts` are **per-game averages**. Field 31 is the season, 32 the player id, and 25 the class (`Fr`/`So`/`Jr`/`Sr`). Season-to-date total minutes = `minutes × games`. 2026: 4,978 rows, 365 teams. |
| ↳ preseason rows | **Last season's, by inference.** On 2026-10-31 the 2027 file held about 1 MB of rows with up to 22 games, which no 2027 game could produce. By 2026-11-01 Torvik had replaced it with the first 2027 data: 19 rows from one Notre Dame–Villanova game, each with `year=2027` and `games=1`. The 10-31 body was not kept, so whether those stale rows carried `year=2026` in field 31 is unknown. The loader keeps only rows whose field 31 equals `season`. Stale rows never make a Monday in season, because §8.2 reads the game logs. |
| `{season}_all_advgames.json.gz`: **player game logs** | 200 `application/json` with `Content-Encoding: gzip`: a JSON list of 53-element lists, one per player per game played. It holds the date (`YYYYMMDD`), the opponent's Torvik name, minutes (index 8), 2PM/2PA/3PM/3PA/FTM/FTA, PTS, ORB/DRB, AST, team, player name and player id. FGM = 2PM + 3PM, FGA = 2PA + 3PA. Only D-I vs D-I games appear. **A player who did not play has no row** (2026: zero 0-minute rows), so DNP (§8.4) means no row for the player on a date when another player on his team has one. Size: 11 MB on the wire and 44 MB decoded at season end, fetched once per run. Before the season's file is published (2027 on 2026-11-01), the URL **302-redirects to `/`**. The client does not follow redirects, and treats that redirect as an empty game log, never as data. |
| `getgamestats.php?year=<season>&csv=1` | **Not player game logs**, as the 10-31 probe assumed. It is one row per **team**-game (31 fields, hoopR's `R/torvik_game_stats.R`). Not used. |
| `playerstat.php?…` (player page) | **Blocked**: 200 `text/html`, `<title>Verifying Browser...</title>`. Used only as a link for the reader (§5). |

**The challenge page returns HTTP 200.** A Torvik response is a source failure (§9.3) when its content type is `text/html` or its body contains `Verifying Browser`, whatever the status. The fixture `tests/fixtures/torvik/playerstat-challenge-*.html` is the page to test against.

**ESPN** (`https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard`, no key):

| Query | Finding |
|---|---|
| `?dates=YYYYMMDD&groups=50&limit=1000` | 200 JSON. `leagues[0].season.year = 2027`. `dates` selects the US-local day, and `events[].date` is UTC, so most evening games carry the next day's UTC date. **Complete**: 29 events for 2026-11-03 (Election Day; opening night is Monday 2026-11-02), the same 29 CBBD lists for that ET day. Broadcasts are at `competitions[].broadcasts[].names`. Teams are at `competitions[].competitors[].team`, with `id`, `location`, and `conferenceId`. **Non-D-I opponents appear, with no `conferenceId`**. |
| `?dates=YYYYMMDD-YYYYMMDD…` | **400** `{"code":400,"message":"Failed to get events endpoint."}`. Ranges are not accepted. |
| `?dates=YYYYMM…` | 200, the whole month, but cut off at `limit` (1,000 events, 6.5 MB, ending 2026-11-19). Not used: a month holds more games than any safe limit, and a week can span two months. |

**CBBD** (`https://api.collegebasketballdata.com`, `Authorization: Bearer <CBBD_API_KEY>`; seasons are end years, so 2027 = 2026-27):

| Endpoint | Finding |
|---|---|
| `GET /teams?season=<season>` | List of 365 D-I teams: `id`, `school`, `displayName`, `conference`, and `sourceId`. **`sourceId` is the ESPN team id**: every D-I team in the ESPN fixture matches one, and no non-D-I team does. |
| `GET /ratings/adjusted?season=<season>` | List of `{season, teamId, team, conference, offensiveRating, defensiveRating, netRating, rankings: {offense, defense, net}}`, one per D-I team. **`[]` for 2027 on 2026-11-01**: ratings exist only once games are played. An empty list is a valid response, and every team is then unranked. |
| `GET /teams/roster?season=<season>` | One request returns every team: 1,544 entries, of which only the 365 D-I teams carry `players` (12 to 20 each). A player has `id`, `sourceId`, `name`, `firstName`, `lastName`, `position`, `height`, `startSeason` and `endSeason`. Filter `team=` exists but is not needed. |
| `GET /games?startDateRange=<ISO>&endDateRange=<ISO>` | Games in a UTC window. `sourceId` is the ESPN event id. Used only for this probe's completeness check. |
| Quota | **1,000 calls** on the free key, shared with CollegeFootballData. The `x-calllimit-remaining` response header read 999 after the first call and 995 after the fifth. The probe did not establish the reset period. It is assumed monthly, as on CollegeFootballData. A season of weekly runs uses about 30 calls. |

**Use per run:**
- **Torvik:** the player season file and the player game logs for `season`. That is 2 requests.
- **CBBD:** adjusted ratings for `season`, which is 1 request. Teams, only when the crosswalk is refreshed. Rosters, in reset mode only (1 request).
- **ESPN:** the scoreboard for this week, as 7 daily requests (ranges are rejected).

A typical run makes 10 requests, under the 20-request budget.

**Politeness and robustness:** one fetch per endpoint per run. Every raw response is cached under `/data/cache/<source>/<key>` for 24 h, so re-running a step inside the window does not refetch. Requests use a shared httpx client (port nba-today `sources/http.py`) with a browser User-Agent and bounded retries. **Never poll.** Each endpoint has a pydantic model, and a shape change fails loudly as a source failure (§9.3), never as a silent partial parse. Fixtures live in `tests/fixtures/<source>/` with the fetch date in the filename.

**Do not use:** `cbbdata` / `api.cbbdata.com` (defunct), KenPom, sportsbook APIs, any login-gated site, or any challenge-solving client (`curl_cffi` impersonation against Torvik included).

## 7. Watchlist

### 7.1 File

```yaml
# watchlist.yaml (repo root)
season: 2027          # season end year
players:
  - name: Tyran Stokes
    team: Kansas      # team as of when the line was added; may be blank
    tier: 1           # 1 must-watch, 2 tracking, 3 fringe
    class: 2027       # expected draft year
    note: "optional free text"
```

**Validation** fails the run loudly, before any fetch:
- `season` is an int.
- `tier` is 1–3.
- `class` ≥ `season`. The brief's example used `class: 2026` with `season: 2027`, which this rule rejects. The 2026 draft has already happened, so a 2026-27 prospect is `class: 2027`.
- `name` is non-empty.
- `team` is optional.
- No two entries share both a normalised name and a team.

Tom seeds 25–40 names in early November from a public big board. That is not the pipeline's job. The pipeline **never writes** `watchlist.yaml`.

### 7.2 Normalisation

Normalising a name means:
- Casefold it.
- Strip accents (NFKD, then drop combining marks).
- Remove `.`, `'` and `’`.
- Collapse whitespace.
- Drop trailing suffixes `jr`, `sr`, `ii`, `iii` and `iv`.

Team names resolve through the crosswalk (§7.5), never by string similarity.

### 7.3 Resolution

Resolution runs on every run against the Torvik player season file for `season`, in this order:

1. **Exact:** normalised name + the stated team's canonical slug match exactly one row → `resolved`.
2. **Team changed:** no match in step 1, but the exact normalised name matches exactly one row on any team → `resolved` + alert `team_changed (was X, now Y)`.
3. **Fuzzy:** still none, and the team is stated: rapidfuzz `token_sort_ratio` ≥ 90 against names on the stated team gives exactly one candidate → `resolved` + alert `fuzzy_match (matched "…")`.
4. **Otherwise:**
   - More than one candidate at any step → `ambiguous`, listing the candidates.
   - No candidate → `unresolved`. Before the player's team has played, the alert says `not yet in Torvik's file (team has not played)`.
   - Never guess.

Results go to `watchlist_resolution`, one row per (season, watchlist key). The watchlist key is the normalised name + the stated team slug, or `—` when the team is blank. A row stores:
- the outcome;
- the Torvik player id, team slug and matched spelling;
- the alert kind;
- the run that first produced this outcome;
- the run that last changed it.

### 7.4 Alerts

Alert kinds:
- `unresolved`, `ambiguous`, `team_changed` and `fuzzy_match`, from resolution;
- `inactive`: the player's team played in each of the last two weeks and he logged 0 minutes;
- `unmapped_team`: a source team name has no crosswalk entry, and the affected rows are dropped from their section.

An alert appears in section 5 **only on the run where its outcome or details first appear or change**. A persistent `unresolved` is reported once, not weekly. A forced re-run of the same week (§9.2) shows the alerts that week originally raised.

### 7.5 Team crosswalk

`teams` holds one row per **canonical team**:
- a slug (e.g. `north-carolina`), seeded from the CBBD teams endpoint for D-I;
- `cbbd_id`, `cbbd_name`;
- `torvik_name`;
- `espn_id`, `espn_name`.

`espn_id` comes from CBBD's `sourceId`, which is the ESPN team id (§6), so ESPN needs no hand mapping. `torvik_name` defaults to CBBD's `school`. Hand-mapped overrides live in the committed `teams_crosswalk.yaml`, keyed by slug, with the field `torvik_name`. Expect about **93** `torvik_name` overrides: that many of the D-I teams common to Torvik 2026 and CBBD 2027 have a Torvik name that differs from CBBD's `school`, mostly `St.` against `State` (`Iowa St.`/`Iowa State`), plus forms like `Connecticut`/`UConn` and `Mississippi`/`Ole Miss`. The overrides are listed explicitly, never derived by a rewrite rule.

An ESPN competitor with no `conferenceId` is a non-D-I team (§6). It is not in the crosswalk and raises no `unmapped_team`. Its game stays a featured-game candidate, with the team shown under its ESPN name, without a link, and counted as unranked. A crosswalk test asserts the mapping against the saved fixtures:
- every ESPN D-I team in the scoreboard fixture maps to exactly one slug;
- every distinct team in the Torvik season-file fixture maps to exactly one slug;
- no two slugs share a source name.

The Torvik season-file fixture is season 2026 and the CBBD teams fixture is season 2027. They differ by one team each way: `Saint Francis` is only in Torvik 2026 and `West Florida` only in CBBD 2027. The test allows exactly those two.

At run time an unmapped name raises `unmapped_team` and is never fuzzily guessed.

## 8. Compute rules

### 8.1 Windows

All times are `America/New_York`.
- For a run on Monday *M*, **last week** is *M*−7 00:00 through *M*−1 23:59:59, and **this week** is *M* 00:00 through *M*+6 23:59:59.
- The run id is `<season>-W<ISO week of M>`, e.g. `2027-W46`.
- Both windows are stored on the `runs` row.

### 8.2 In season

A Monday is **in season** if either:
- Torvik game logs hold at least one game dated within last week; or
- ESPN's scoreboard holds at least one D-I game in this week.

Out of season, the run records `status=skipped_offseason`, pings healthchecks and sends nothing. So the first send is the Monday of opening week (games this week only: section 2 is empty, section 4 shows the baseline line). The last send is the Monday after the national title game (recap only, with no featured games). No configured dates are involved.

### 8.3 Featured games

Candidates are ESPN games in this week where at least one team is the current team of a `resolved` watchlist player. Sort keys:
1. Best (lowest) tier among watchlist players in the game ↑.
2. Number of resolved watchlist players on either team ↓.
3. The worse (numerically higher) of the two teams' ranks ↑. A game is as good as its weaker team; an unranked or unmapped team counts as rank 999.
4. Tip time ↑.

Show the top 5, then `+N more`. Networks come from `broadcasts[].names`, joined with `/`. A game with no broadcast shows `—`.

### 8.4 Notable

Evaluated per player over last week's games. The first rule that matches, in this order, is the only flag shown:
1. `DNP` / `DNP — 2nd straight week`: the team played at least one game last week and the player logged 0 minutes in all of them. Torvik writes no game-log row for a player who did not play (§6), so 0 minutes means no row for him on a date with rows for his team. Such a player *does* get a row; it is the one exception to "played". From the second straight week the player also raises `inactive` (§7.4).
2. `Season high: N pts`: a game last week set the player's season high in points, with at least 3 season games played before it.
3. `0-for-N`: a game with 0 FGM on ≥ 5 FGA, or 0 3PM on ≥ 5 3PA. Show the larger N, labelled `FG` or `3P`.

There is no `career high` and no `injury` flag in v1.

### 8.5 Freshmen to watch

From the Torvik season file: players with class year `Fr` whose team's current rank is ≤ 100, excluding every resolved watchlist player. Sort by season-to-date total minutes ↓, then take 5. Show team and rank, MPG, PTS/REB/AST per game and TS%.

### 8.6 Movers

Compare this run's ratings snapshot top 25 with the previous run's snapshot in the same season. Show:
- every team in either top 25 whose rank changed by ≥ 3 places;
- every team that entered the top 25;
- every team that left it.

Sort by |change| ↓, with entries and exits after the moves, by current rank. Cap at 10. A same-week forced re-run compares with the previous **week's** snapshot, not with itself.

## 9. Architecture

### 9.1 Flow and CLI

There is one package, `cbb_digest`, in the `src` layout. `typer` CLI `cbb-digest`:

| Command | Does |
|---|---|
| `fetch` | Fetch every source for the run window into `/data/cache` (24 h cache). |
| `load` | Parse the cached raw files through the pydantic models. Upsert into SQLite by natural key, so a second `load` changes zero rows. Snapshot the ratings for the run id. |
| `resolve` | Watchlist → `watchlist_resolution` (§7.3). |
| `compute` | Build the typed `Digest` model (§8). |
| `render [--dry-run]` | Digest → HTML + text. `--dry-run` writes `out/digest.html` and `out/digest.txt` (under `/data/out` in the container). |
| `send [--force]` | Send through the transport chain. Refuses if the run id already has `sent_at`, unless `--force`. |
| `run [--week YYYY-MM-DD] [--force] [--dry-run]` | All of the above for one week, plus the healthchecks ping. |
| `serve` | Long-running loop (§9.2). |
| `reset --season N [--send]` | Portal-reset digest (§11). Without `--send`, it writes to `out/`. |
| `health` | Container health check: exit 0 if the last `serve` heartbeat in SQLite is under 8 days old. |

Every step can be re-run alone. Steps after `fetch` read only from the cache and the database. Port these from nba-today: `settings.py`, `logging.py`, `clock.py`, `heartbeat.py`, `sources/http.py`, `transport/`, `render/` and `atomic.py`. Adapt `scheduler.py`.

### 9.2 Scheduling and idempotency

`serve`, on boot and after each run:
1. Compute the next Monday 07:00 ET.
2. If it is currently Monday 07:00 through Tuesday 12:00 ET and the current run id has no `sent_at` and is not `skipped_offseason`, run now. A missed Monday catches up until Tuesday noon. After that the week is skipped and logged, and healthchecks has already alarmed.
3. Sleep until the next Monday 07:00 ET.

The run id is `season + ISO week`. Re-running the same week overwrites that week's ratings snapshot and resolution rows. It sends again **only** with `--force`. `TZ=America/New_York` is set in the image, and all scheduling uses `zoneinfo`, so DST needs no attention.

### 9.3 Failure handling

Each source fetch is independent. If any source fails (HTTP error, shape-validation error, or the challenge page detected by content type or markup), the run waits **30 minutes** and refetches only the failed sources. Anything still failing becomes a banner line (§5), and the dependent sections are omitted. A week-old cache or snapshot is **never** substituted for stats, ratings or schedule. The run's status is `sent_degraded`. If Torvik fails, resolution is skipped: the previous `watchlist_resolution` rows stand as player identities (no new alerts), and the watchlist and freshmen sections are omitted. If CBBD fails, ranks show as unranked and movers is omitted.

If the email cannot be sent through any transport, or an unexpected exception occurs, the run is recorded as `failed` and healthchecks gets `/fail` with the reason. The email itself never reports the app's own crash, because healthchecks does.

### 9.4 Data model (SQLite, Alembic batch mode)

| Table | Key | Holds |
|---|---|---|
| `teams` | `slug` | The crosswalk (§7.5). |
| `team_ratings_snapshot` | (`run_id`, `slug`) | rank, net, adj_o, adj_d, fetched_at. |
| `players` | (`season`, `torvik_pid`) | name, normalised name, team slug, class year, season totals used by §8.4–8.5. |
| `player_games` | (`season`, `torvik_pid`, `game_date`, `opponent_slug`) | min, pts, reb, ast, fgm, fga, 3pm, 3pa, fta, and what TS% needs. |
| `games` | `espn_id` | start_utc, home_slug, away_slug, ESPN's home_name and away_name (§7.5), networks, venue, season. |
| `watchlist_resolution` | (`season`, `watchlist_key`) | §7.3 fields. |
| `runs` | `run_id` | kind (`digest`/`reset`), season, both windows, status (`running`/`sent`/`sent_degraded`/`skipped_offseason`/`skipped_missed`/`failed`), started_at, finished_at, duration, per-source row counts, failed sources, alerts raised (JSON), image SHA, sent_at, transport used. |
| `heartbeat` | singleton | last `serve` loop tick (for `health`). |

Migrations are reviewed by hand. SQLite autogenerate can emit a stray `drop_table` for `_alembic_tmp_*` tables. Every migration must be idempotent.

### 9.5 Configuration

All configuration is environment variables through pydantic-settings; `.env.example` documents each one.

| Variable | Purpose |
|---|---|
| `DATA_DIR` | Default `/data`. |
| `CBBD_API_KEY` | Required. |
| `RESEND_API_KEY`, `RESEND_FROM` | `RESEND_FROM` is `CBB Digest <cbb@tomboone.com>`. |
| `SMTP_*` | Optional fallback, as in nba-today. |
| `RECIPIENTS` | `tom@tomboone.com`. |
| `HEALTHCHECKS_URL` | Ping URL. |
| `DRY_RUN` | Default `false`. |
| `IMAGE_SHA` | Baked at build time. |

The season comes from `watchlist.yaml`, not an environment variable.

## 10. Operations

- **Image:** a multi-stage uv Dockerfile running as non-root UID/GID 999. `TZ=America/New_York`. `watchlist.yaml` and `teams_crosswalk.yaml` are copied in. `IMAGE_SHA` is a build arg. `HEALTHCHECK` is `cbb-digest health`.
- **Workflows:** `.github/workflows/ci.yml` (ruff, format, pyright, pytest) and `image.yml` (push to `main` → GHCR `:latest`, `:sha-<7>`). Copy both from nba-today.
- **Deploy:** `deploy/docker-compose.yml` with `cbb-digest` (`restart: unless-stopped`, `./data:/data`, `env_file: .env`) and a `nickfedor/watchtower:1.23.2` scoped to `cbb-digest`. `deploy/README.md` is a runbook in nba-today's shape (sparse clone, `chown 999:999 data`, healthchecks setup, `.env`, first run with `DRY_RUN=true`, going live). Commands on kumadrome needing `sudo` are written for Tom to run.
- **Backup:** `deploy/backup/cbb-digest-backup.{sh,service,timer,env.example}`, cloned from closinglineup.
  - The script uses `sqlite3 /…/data/cbb-digest.sqlite3 ".backup '<tmp>'"`, then `PRAGMA integrity_check` on the copy, then an atomic rename.
  - Dumps go to `/mnt/music/cbb-digest/backups`, are kept for 14 days, and are `rclone sync`ed to `b2:<bucket>/cbb-digest-backups`.
  - Pushes go to a Kuma push monitor (heartbeat 25 h).
  - It runs daily at 05:30 America/New_York with `RandomizedDelaySec`, and checks `mountpoint -q` first.
- **Monitoring:** one healthchecks.io check (period 7 days, grace 26 h). Its own email integration is the failure alert.
- **Done for v1 operations:** two consecutive Mondays arrive without manual intervention.

## 11. Portal-reset digest (final milestone, build March 2027)

`cbb-digest reset --season 2028` runs on demand, by hand, in late April and again after the NBA early-entry withdrawal deadline in late May:

```
docker compose exec cbb-digest cbb-digest reset --season 2028 [--send]
```

It does the following:
1. Fetch CBBD rosters for the new season and, if it has rows, Torvik's new-season player season file. The new-season player file is optional, because it is usually empty in April.
2. For each watchlist player, compare the last resolution of the old season (team slug, Torvik id) with the new-season rosters. Match by normalised name; within one team, the §7.3 steps apply.
3. Assign a **reset status**:
   - `same`: found on the same team;
   - `transferred`: found on exactly one other team;
   - `not_on_roster`: found on no new-season roster (declared, graduated, unsigned or simply not yet listed; Tom decides which);
   - `unresolved`: ambiguous, or never resolved last season.
4. Render the reset digest. It uses the same template frame, but sections 1–4 are replaced by one table: player, tier, class, last-known team, current team, reset status, and a Torvik link. Sections 5 and 6 stay.
5. Write a **proposed** `out/watchlist-<season>.yaml`:
   - `season` is bumped;
   - only players with `class ≥ new season` are kept;
   - `team` is set to the detected current team, or blank for `not_on_roster`/`unresolved`;
   - tier, class and note are preserved.

   With `--send`, the file is attached to the email. Tom edits it and commits it by PR. The pipeline still never writes the repo.

   The ported `Transport.send` gains an optional `attachments: list[Attachment(filename, content: bytes, mime)]`. Resend passes them in its `attachments` field (base64), SMTP uses `EmailMessage.add_attachment`, and the outbox writes them next to the HTML. The weekly digest never attaches anything.

A reset run is a `runs` row with `kind=reset` and run id `reset-<season>-<date>`. It has no healthchecks ping, because it is manual. **Done:** run against the 2027 portal window, it produces a table Tom can edit the watchlist from in under 30 minutes.

## 12. Build order (milestones)

Linear: project P-NEU-106, stories NEU-2081..2087, tickets NEU-2088..2120. NEU-2118 carries `planit`. The runbook tickets (NEU-2096, 2105, 2112, 2117, 2120) are Tom's manual steps.

1. **Scaffold & deploy skeleton.** The repo, toolchain, CI, image, and the `serve`/`health` skeleton running on kumadrome, sending nothing.
2. **Sources & load.** Fixtures, models, clients, crosswalk, schema, and upserting loaders.
3. **Watchlist & resolution.**
4. **Digest compute, render & send.** The first real email.
5. **Unattended weekly operation.** The scheduler, failure handling, healthchecks, backup, and the two-Monday soak.
6. **Portal-reset mode** (March 2027).

## 13. Deferred and open

- **Deferred:** career highs (needs backfill), injury flags, a stale-snapshot fallback, shared code with the NBA apps, a web view, and multi-recipient sending.
- **Probed in milestone 2** (NEU-2093, 2026-11-01): Torvik preseason rows and column maps, ESPN completeness and range support, and the CBBD endpoints and quota. The results are in §6 and §7.5. Still open, because the probe cannot reach them:
  - the Torvik link forms (§5), which sit behind the browser check: click one of each in the first dry-run digest;
  - the CBBD quota reset period.
