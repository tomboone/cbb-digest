# CBB Weekly Digest — Handoff for Claude Code

Oct 31, 2026 · @Tom

## Goal & scope

Build `cbb-digest`: a cron job that emails Tom one Monday-morning summary of men's college basketball, filtered through a personal watchlist of NBA draft prospects. Intended use: hand this doc to `/projectit` in Claude Code to produce the Linear project and tickets.

**Why this exists.** Tom follows the NBA, not college. He wants to know (a) which 25–40 college players matter to next June's draft, (b) what they did last week, (c) which one or two games this week are worth watching, and (d) who moved in the top-25 ratings. Existing sites cover each of these separately; nothing merges them around a personal list.

**What it is**

- One email per week, Monday \~7:00 ET, in season (early November through the Final Four).
- One "portal reset" email run on demand in late April and again after the NBA early-entry withdrawal deadline in late May.
- Single user, private, free data sources only, runs on the Coolify box.

**What it is not**

- Not a dashboard or web UI. If Tom wants to look at something, the email links out to Torvik/ESPN.
- Not a news aggregator. No scraping of articles, no RSS.
- Not a betting or fantasy tool.
- Not a replacement for the \[\[nba-rotation-dashboard\]\]. Shared code is fine later; separate repo and deploy now.

**Success test.** By February, Tom is still opening the email and has picked at least one game a week from it. If not, the project is shelved, not expanded.

## Email contents

Five sections, in this order, plain HTML with a text fallback. Target length: readable in under five minutes on a phone. Every player and team name links to its Torvik page.

| # | Section | Content | Source |
| --- | --- | --- | --- |
| 1 | This week's games | 3–5 games in the next 7 days involving watchlist players, ranked by (watchlist players on court) × (opponent Torvik rank). Date, time ET, network, both teams' Torvik ranks. | ESPN scoreboard + Torvik ratings |
| 2 | Watchlist last week | One row per watchlist player who played: team, games, MPG, PTS/REB/AST, TS%, 3PA, and a one-line "notable" flag (career high, DNP, injury, 0-for-X). Sorted by Tom's watchlist tier, then by minutes. | Torvik player game logs |
| 3 | Top-25 movers | Torvik top 25 with rank change vs. last Monday. Show only the rows that moved ≥3 spots or entered/left; link to the full table. | Torvik `trank` JSON, snapshotted weekly |
| 4 | Watchlist alerts | Anything the pipeline could not resolve or that changed: player not found, team changed, zero minutes for 2+ weeks, name collision. Empty section is omitted. | Pipeline state |
| 5 | Footer | Run timestamp, data as-of date, link to the watchlist file in the repo. | — |

**Portal-reset email** (April/May, manual trigger): same template, sections 1–3 replaced by one table — each watchlist player, last-known team, current team per Torvik/CBBD roster, status (`same`, `transferred`, `declared`, `unresolved`). Tom edits the watchlist from this.

**Formatting rules**

- Stats to one decimal; percentages as whole numbers.
- No images. No tracking pixels. Subject line: `CBB digest — week of Nov 9` (Monday's date).
- If a data source failed, say so in a single line at the top and send what rendered rather than skipping the week.

## Data sources

Three free sources, all unofficial or lightly documented. Treat every URL below as "verify on first run": the first ticket in Phase 1 is to confirm each endpoint's shape and save a fixture.

| Source | Supplies | Access | Notes |
| --- | --- | --- | --- |
| [Bart Torvik](https://barttorvik.com/) | Team ratings (T-Rank, AdjO/AdjD, Barthag), player season stats, player game logs, schedules with pregame win probability | No key. Plain GET on `.php` endpoints with `json=1` or `csv=1` | The canonical URL list is in hoopR's `torvik_*` wrappers ([hoopR NEWS](https://ftp.fau.de/cran/web/packages/hoopR/news/news.html)) — read `R/torvik_*.R` in that repo for exact paths and params. Known entry points: `trank.php?year=2027&json=1` (team ratings), `getadvstats.php?year=2027&csv=1` (player season), `getgamestats.php?year=2027&csv=1` (player game logs). Send a browser User-Agent. Year param = season end year (2026-27 → `2027`). |
| [CollegeBasketballData](https://collegebasketballdata.com/) (CBBD) | Teams, rosters, games, player season stats, lines | Free Bearer token via `CBBD_API_KEY` | Documented REST API, the men's basketball sibling of CollegeFootballData. Use for rosters (portal-reset mode) and as fallback if a Torvik file changes shape. |
| ESPN scoreboard (unofficial) | Game times, TV network, venue, team logos | No key. `site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates=YYYYMMDD&groups=50&limit=400` | `groups=50` = all D-I. Broadcast info is under `competitions[].broadcasts[]`. Only source for TV network. |

**Do not use:** the `cbbdata` R package / `api.cbbdata.com` (defunct per hoopR), KenPom (paid; scraping violates ToS), any sportsbook API, any site requiring login.

**Entity matching.** Torvik, CBBD and ESPN each have their own team names and player name spellings. Build one `teams` table keyed by a canonical slug with a column per source name; seed it from Torvik's team list and hand-map the mismatches (expect \~30). Player matching is name + team within a season; see Watchlist.

**Politeness.** One fetch per endpoint per run, cached to disk for 24 h. Never poll. Total weekly load should be under 20 requests.

## Watchlist

The watchlist is a YAML file in the repo, hand-edited by Tom, and is the only input the pipeline needs from him. The pipeline never writes to it; it writes resolution results to the database and reports problems in the email.

```yaml
# watchlist.yaml
season: 2027          # season end year, matches Torvik's year param
players:
  - name: Jordan Smith Jr.
    team: Arkansas     # team as of when Tom added the line
    tier: 1            # 1 = must-watch, 2 = tracking, 3 = fringe
    class: 2026        # expected draft year
    note: "lottery guard, check 3P% trend"
  - name: Tyran Stokes
    team: Kansas
    tier: 1
    class: 2026
```

**Seeding.** Tom writes the first 25–40 names in early November from a public big board (ESPN/Givony, CBS/Finkelstein). Not the pipeline's job.

**Resolution** (runs every job, results stored in `watchlist_resolution`):

1. Exact match on normalized name + team in Torvik's player season file for `season`.
2. If none: exact name, any team → resolve and flag `team_changed` in the Alerts section with the new team.
3. If none: fuzzy name (token-sort ratio ≥ 90) within the stated team → resolve and flag `fuzzy_match` with the matched spelling.
4. If still none, or more than one candidate: leave unresolved, flag `unresolved` or `ambiguous`. Never guess.

Store the resolved Torvik player id and team per player per season. Re-run resolution each week but only re-flag when the result changes.

**Cross-season continuity.** When `season` increments, carry forward every player whose `class` is still in the future, with `team` blanked so resolution step 2 finds the new school. The portal-reset email is the human review step for this.

## Architecture & stack

One container, one command, one Postgres. No web server, no queue, no framework beyond what renders the email.

**Flow per run:** `fetch` (Torvik, CBBD, ESPN → raw JSON/CSV on disk) → `load` (parse into Postgres tables) → `resolve` (watchlist → player ids) → `compute` (week window, movers vs. last snapshot, game ranking) → `render` (Jinja2 → HTML + text) → `send` (Resend) → `ping` (healthchecks). Each step is a subcommand so any one can be re-run alone: `cbb-digest fetch`, `cbb-digest render --dry-run`, etc.

| Concern | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.12, `uv` for deps | Matches Tom's other projects |
| HTTP | `httpx` with retries, 24 h on-disk cache in `/data/cache` | Few requests, politeness |
| Parsing/validation | `pydantic` models per source | Endpoints are undocumented; fail loudly on shape change |
| Storage | Postgres 17 in Coolify (own container, like his other apps); `sqlalchemy` core + `alembic` | Fits his existing per-container backup script to the Hetzner Storage Box |
| Tables | `teams`, `team_ratings_snapshot` (weekly), `players`, `player_games`, `games`, `watchlist_resolution`, `runs` | Snapshots are what make "movers" possible |
| Templating | Jinja2, one HTML template + one text template, inline CSS | No build step |
| Email | Resend, its own free account per his convention; from `digest@neuroticsasquat.ch` | DNS already at Cloudflare; needs SPF/DKIM records for the subdomain |
| Scheduling | Coolify scheduled task on the resource: `0 11 * * 1` UTC (07:00 ET Monday; adjust for DST or use `TZ=America/New_York` in the container) | No separate cron host |
| Monitoring | healthchecks.io ping on success/fail; structured logs to stdout; optional OTLP to his SigNoz | Silent failure is the main risk for a weekly job |
| Config | Env vars: `DATABASE_URL`, `RESEND_API_KEY`, `CBBD_API_KEY`, `HC_PING_URL`, `TO_EMAIL`, `SEASON` | 12-factor, Coolify-native |
| Repo | New repo under the neuroticsasquat-ch org; Dockerfile; `make dry-run` renders to `./out/digest.html` | Local preview without sending |

**Week window.** "Last week" = Monday 00:00 ET of the prior week through Sunday 23:59 ET. "This week" = the 7 days starting the run's Monday. Store the window on the `runs` row.

**Idempotency.** A run id is `season + ISO week`. Re-running the same week overwrites that week's snapshot and re-sends only with `--force`.

## Build plan

Five phases, each shippable on its own. Phase 3 is the first one that delivers value; the season has already started by then, which is fine. Suggested ticket granularity: one Linear issue per bullet.

**Phase 0 — Scaffold** (half a day)

- Repo, `uv` project, Dockerfile, `cbb-digest` CLI entry point with `fetch/load/resolve/compute/render/send/run` subcommands stubbed.
- Postgres resource in Coolify, `alembic` initial migration for the seven tables.
- Done when: `docker run cbb-digest --help` works and `alembic upgrade head` runs against the Coolify DB.

**Phase 1 — Fetch & load** (1–2 days)

- Confirm each endpoint in Data sources; save one fixture per endpoint under `tests/fixtures/` with the fetch date.
- `pydantic` models for Torvik team ratings, player season, player game logs; CBBD teams/rosters; ESPN scoreboard.
- `teams` canonical table + hand-mapped name crosswalk; a test that every Torvik team maps to exactly one ESPN team.
- Loaders are upsert-by-natural-key; re-running a fetch is safe.
- Done when: a full `fetch && load` for the current week populates all tables and a second run changes zero rows.

**Phase 2 — Watchlist resolution** (half a day)

- YAML loader with validation (tier 1–3, class ≥ season).
- The four-step resolver; results to `watchlist_resolution`; unit tests for each step using fixture names, including a deliberate ambiguity.
- Done when: Tom's real watchlist resolves with every unresolved name explained in the output.

**Phase 3 — Render & send** (1 day)

- `compute`: week window, per-player last-week lines, "notable" flags, top-25 diff vs. prior snapshot, game ranking for the coming week.
- Jinja2 HTML + text templates; `render --dry-run` writes to `./out/`.
- Resend integration; subdomain DNS (SPF, DKIM) at Cloudflare.
- Done when: Tom receives a real email for the current week and it renders correctly in Fastmail and on his phone.

**Phase 4 — Schedule & observe** (half a day)

- Coolify scheduled task; healthchecks.io check with a 26 h grace period; failure email from healthchecks, not from the app.
- `runs` table records status, window, row counts, duration.
- A source failure degrades to a one-line notice at the top of the email rather than aborting.
- Done when: two consecutive Mondays arrive without manual intervention.

**Phase 5 — Portal-reset mode** (1 day, build in March)

- `cbb-digest reset --season 2028`: pull CBBD rosters + Torvik's new-season player file, compare to last resolution, emit the reset table.
- Carry-forward logic for the watchlist across seasons.
- Done when: run against the 2027 portal window produces a table Tom can edit the watchlist from in under 30 minutes.

**Explicitly deferred:** anything with a browser, per-game box-score emails, historical backfill beyond the current season, shared code with the NBA dashboard.

## Constraints, non-goals, open questions

**Constraints**

- Free data only. No KenPom, no paid APIs, no scraping behind logins.
- Runs on the existing Coolify host; no new VM, no new SaaS beyond a Resend account and a healthchecks check.
- Total weekly external requests under 20. Cache everything for 24 h.
- Undocumented endpoints will change shape mid-season. Every parser fails loudly, every failure degrades the email rather than suppressing it.
- Single recipient. No unsubscribe, no auth, no multi-tenancy.

**Non-goals**

- Web UI, dashboard, mobile app.
- News, rumors, recruiting, NIL, coaching changes.
- Women's basketball, non-D-I, international prospects not on a college roster.
- Predictions or betting content.
- Real-time or per-game alerts.

**Open questions for Tom** (answer before Phase 3; defaults in parentheses)

- [ ] Send-to address: `tom@tomboone.com` or the new neuroticsasquat.ch Workspace inbox? (personal)
- [ ] Postgres or SQLite-on-volume? Postgres matches the backup script; SQLite is one less container. (Postgres)
- [ ] Game ranking weight: should a tier-1 player's game always outrank a tier-2 matchup regardless of opponent quality? (yes)
- [ ] Include a "freshman of note not on your list" row — top-5 Torvik freshman by minutes not already watched? (no for v1)
- [ ] Hard cap on email length: 5 games / 40 players / 10 movers? (yes, with a "+N more" link to Torvik)
- [ ] Season end: stop after the national title game, or keep running through the draft with a reduced template? (stop; reset mode covers April–May)

**Sources consulted**

- [hoopR NEWS — torvik\_\* and cbbd\_\* families](https://ftp.fau.de/cran/web/packages/hoopR/news/news.html)
- [cbbdata (GitHub, superseded)](https://github.com/andreweatherman/cbbdata/)
- [NCAA.com — 76-team field from 2026-27](https://ncaa.com/news/basketball-men/article/2026-10-05/andy-katzs-most-impactful-players-transfers-2026-27-mens-college-basketball)
- [2026 portal key dates](https://www.sportsgrid.com/ncaab/article/college-basketball-2026-transfer-portal-big-board-rankings-1-50)
