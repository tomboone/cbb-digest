# Source fixtures

Real responses saved by the source probe of 2026-11-01 (NEU-2093). The filename is
`<endpoint>[-y<season>]-<fetch date>.<ext>`. `-y<season>` appears whenever the request names a season. Requests that name a date window instead (ESPN scoreboard, CBBD games) carry no season token, and this table gives their window. Findings and decisions are in spec §6. This file
records what each fixture is, how it was fetched, and how it was trimmed.

All requests used curl with a desktop Chrome User-Agent and `--compressed`. CBBD requests sent
`Authorization: Bearer <CBBD_API_KEY>`.

## Request log (15 requests)

| # | Time (UTC) | Status | Request | Fixture |
|---|---|---|---|---|
| 1 | 20:37:33 | 200 `text/csv` | Torvik `getadvstats.php?year=2027&csv=1` | `torvik/getadvstats-y2027-2026-11-01.csv` |
| 2 | 20:37:59 | 200 `text/csv` | Torvik `getadvstats.php?year=2026&csv=1` | `torvik/getadvstats-y2026-2026-11-01.csv` (trimmed) |
| 3 | 20:38:10 | 200, empty body | Torvik `getgamestats.php?year=2027&csv=1` | none |
| 4 | 20:38:10 | 302 → `/` | Torvik `2027_all_advgames.json.gz` | none |
| 5 | 20:38:29 | 200 `text/csv` | Torvik `getgamestats.php?year=2026&csv=1` | none (team-game file, not used) |
| 6 | 20:38:29 | 200 `application/json` | Torvik `2026_all_advgames.json.gz` | `torvik/all_advgames-y2026-2026-11-01.json` (trimmed) |
| 7 | 20:39:17 | 200 `text/html`, bot check | Torvik `playerstat.php?link=y&year=2026&p=Bennett%20Stirtz&t=Iowa` | `torvik/playerstat-challenge-2026-11-01.html` |
| 8 | 20:39:24 | 200 | ESPN `scoreboard?dates=20261103&groups=50&limit=1000` | `espn/scoreboard-2026-11-01.json` |
| 9 | 20:39:24 | 400 | ESPN `scoreboard?dates=20261103-20261109&groups=50&limit=1000` | none (`{"code":400,"message":"Failed to get events endpoint."}`) |
| 10 | 20:39:40 | 200 | ESPN `scoreboard?dates=202611&groups=50&limit=1000` | none (6.5 MB, truncated at 1,000 events) |
| 11 | 20:46:32 | 200 | CBBD `/teams?season=2027` | `cbbd/teams-y2027-2026-11-01.json` |
| 12 | 20:46:42 | 200, `[]` | CBBD `/ratings/adjusted?season=2027` | `cbbd/ratings-adjusted-y2027-2026-11-01.json` |
| 13 | 20:46:50 | 200 | CBBD `/ratings/adjusted?season=2026` | `cbbd/ratings-adjusted-y2026-2026-11-01.json` |
| 14 | 20:47:01 | 200 | CBBD `/teams/roster?season=2027` | `cbbd/teams-roster-y2027-2026-11-01.json` (trimmed) |
| 15 | 20:47:01 | 200 | CBBD `/games?startDateRange=2026-11-03T05:00:00Z&endDateRange=2026-11-04T04:59:59Z` | `cbbd/games-2026-11-01.json` |

ESPN fixture 8 is the schedule for Tuesday 2026-11-03 ET (29 games), not for the fetch date.

## Trimming

The **sample teams** are Iowa, Duke, Notre Dame, Villanova, Tennessee and Kentucky. Their names
are the same in Torvik, CBBD and ESPN.

- `torvik/getadvstats-y2026-…csv`: every row of the sample teams plus the first row of every
  other team, so all 365 Torvik team names are present (440 of 4,978 rows). Rows are byte-for-byte
  as served.
- `torvik/all_advgames-y2026-…json`: every row whose team (index 47) is a sample team (2,074 of
  113,882 rows), re-serialised compactly. The full file is 43.8 MB decoded.
- `cbbd/teams-roster-y2027-…json`: the six sample teams plus the first three entries with no players
  (non-D-I teams), re-serialised compactly (9 of 1,544 entries).

All other fixtures are the response body as served, after gzip decoding.

## Column maps

### Torvik `getadvstats.php` (player season file): headerless CSV, 67 fields

Matches hoopR's `R/torvik_player_stats.R`, checked against Bennett Stirtz's 2026 row. `games`
counts games played. `ftm`/`fta`/`two_pm`/`two_pa`/`three_pm`/`three_pa` are **season totals**.
`minutes`, `oreb`, `dreb`, `treb`, `ast`, `stl`, `blk` and `pts` are **per-game averages**.
`year` is the season. `class` is `Fr`/`So`/`Jr`/`Sr`. Field 66, which hoopR names
`recruit_date`, holds a birth date (`2003-10-03` for Stirtz).

```
 0 player_name   1 team          2 conf          3 games         4 min_pct
 5 o_rtg         6 usage         7 e_fg          8 ts_pct        9 orb_pct
10 drb_pct      11 ast_pct      12 to_pct       13 ftm          14 fta
15 ft_pct       16 two_pm       17 two_pa       18 two_p_pct    19 three_pm
20 three_pa     21 three_p_pct  22 blk_pct      23 stl_pct      24 ftr
25 class        26 height       27 number       28 porpag       29 adj_oe
30 pfr          31 year         32 player_id    33 hometown     34 rec_rank
35 ast_to       36 rim_made     37 rim_attempts 38 mid_made     39 mid_attempts
40 rim_pct      41 mid_pct      42 dunks_made   43 dunks_attempts 44 dunks_pct
45 pick         46 drtg         47 adrtg        48 dporpag      49 stops
50 bpm          51 obpm         52 dbpm         53 gbpm         54 minutes
55 ogbpm        56 dgbpm        57 oreb         58 dreb         59 treb
60 ast          61 stl          62 blk          63 pts          64 role
65 threat       66 recruit_date
```

### Torvik `{year}_all_advgames.json.gz` (player game logs): JSON list of 53-element lists

One row per player per game played. The names follow dbcjason/NCAACards'
`fetch_bt_advgames_by_year.py`. Index 8, which that script calls `Min_per`, is **minutes
played**: values run 0.1 to 54.1, and the top of that range is an overtime game. Box counts are
per game. FGM = `twoPM + TPM`, FGA = `twoPA + TPA`, REB = `ORB + DRB`. No row exists for a
player who did not play: across the 2026 file there are no 0-minute rows.

```
 0 numdate (YYYYMMDD)  1 datetext   2 opstyle   3 quality   4 win1
 5 opponent (Torvik name)  6 muid   7 win2      8 minutes   9 ORtg
10 Usage     11 eFG      12 TS_per   13 ORB_per  14 DRB_per
15 AST_per   16 TO_per   17 dunksmade 18 dunksatt 19 rimmade
20 rimatt    21 midmade  22 midatt   23 twoPM    24 twoPA
25 TPM       26 TPA      27 FTM      28 FTA      29 bpm_rd
30 Obpm      31 Dbpm     32 bpm_net  33 pts      34 ORB
35 DRB       36 AST      37 TOV      38 STL      39 BLK
40 stl_per   41 blk_per  42 PF       43 possessions 44 bpm
45 sbpm      46 loc (H/A/N) 47 tt (team)  48 pp (player name)  49 inches
50 cls       51 pid      52 year
```

### Torvik `getgamestats.php`: headerless CSV, 31 fields, one row per **team**-game

Not used. Names follow hoopR's `R/torvik_game_stats.R`: `date, type, team, conf, opp, venue,
result, adj_oe, adj_de, oe, off_efg, off_to, off_or, off_ftr, de, def_efg, def_to, def_or,
def_ftr, game_score, opp_conf, quad, year, tempo, muid, coach, opp_coach, margin, win_prob,
game_stats, overtimes`.

## Torvik link forms (spec §5)

The pipeline never fetches these pages, because the player page returned the browser check
(request 7). The forms come from a GitHub code search on 2026-11-01 (`gh search code
"barttorvik.com/playerstat.php"` and `"barttorvik.com/team.php"`). The most common forms were
`playerstat.php?year=<season>&p=<name>&t=<team>` and `team.php?team=<team>&year=<season>`, with
names URL-encoded.
