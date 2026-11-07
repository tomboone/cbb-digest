# cbb-digest

A weekly email that summarises men's Division I college basketball through a personal watchlist of NBA draft prospects, for one reader who follows the NBA rather than college.

## Language

### The watchlist

**Watchlist**:
The hand-edited list of draft prospects for one season (`watchlist.yaml`). It is the only input the reader supplies, and the pipeline never writes to it.
_Avoid_: roster, big board, list

**Watchlist player**:
One entry on the watchlist: a name, the team as of when it was added, a tier, and a class.
_Avoid_: prospect (when the entry, not the person, is meant), target

**Tier**:
The reader's priority for a watchlist player: 1 = must-watch, 2 = tracking, 3 = fringe. Lower is more important.
_Avoid_: rank, priority

**Class**:
The draft year a watchlist player is expected to enter.
_Avoid_: year, draft class (when one player's value is meant)

**Season**:
A college season identified by the calendar year it ends in. 2026-27 is season 2027.
_Avoid_: year (unqualified)

**Resolution**:
Matching a watchlist player to one player in Torvik's player season file for the season. Its outcome is resolved, resolved with a flag, unresolved or ambiguous. A guess is never an outcome.
_Avoid_: lookup, matching

**Alert**:
A watchlist problem or change the email reports: unresolved, ambiguous, team changed, fuzzy match, inactive, unmapped team. An alert is raised only on the run where its outcome or its details first appear or change, so a standing problem is reported once.
_Avoid_: warning, error

**Inactive**:
A resolved watchlist player whose team played in each of the last two weeks while the player logged no minutes.
_Avoid_: injured

### The email

**Digest**:
The Monday email: featured games, watchlist last week, freshmen to watch, top-25 movers, alerts and footer.
_Avoid_: newsletter, report

**Portal-reset digest**:
The on-demand spring email that compares each watchlist player's last-known team with the new season's rosters, and proposes next season's watchlist.
_Avoid_: reset report, offseason email

**Reset status**:
A watchlist player's outcome in a portal-reset digest: same, transferred, not on roster, or unresolved. "Declared" is not a reset status, because no source reports it.

**Featured game**:
A game in this week involving the team of at least one resolved watchlist player, ranked by best tier, then number of watchlist players, then the worse of the two teams' ranks.
_Avoid_: game of the week, pick

**Notable**:
The single most significant flag on a watchlist player's row last week: season high, DNP or 0-for-X.
_Avoid_: highlight

**Freshman to watch**:
One of the five freshmen not on the watchlist with the most season minutes on a team ranked 100th or better.
_Avoid_: sleeper

**Mover**:
A top-25 team whose rank changed by three or more places since the previous snapshot, or that entered or left the top 25.

### Time and runs

**Last week**:
Monday 00:00 through Sunday 23:59 Eastern of the week before a run.

**This week**:
The seven days starting on the Monday of a run.

**In season**:
A Monday on which last week or this week contains at least one D-I game. A Monday out of season sends nothing.

**Run**:
One attempt to produce a week's digest, identified by season plus ISO week. Re-running a week overwrites its snapshot, and sends again only when forced.

**Ratings snapshot**:
The team ratings as captured by a run, kept so that later runs can find movers.

**Rank**:
A team's position by CBBD adjusted net rating at a snapshot.
_Avoid_: T-Rank, Torvik rank, AP rank

**Canonical team**:
A team as cbb-digest identifies it, keyed by a slug, with the name or id each source uses for it.
