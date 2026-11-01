# Team ratings come from CBBD; Torvik supplies players only

The handoff took team ratings from Torvik's `trank.php?json=1`. A probe on 2026-10-31 found that endpoint returns a "Verifying Browser" JavaScript challenge to non-browser clients, while the player season file `getadvstats.php?csv=1` answered a plain GET. cbb-digest does not work around bot checks, so CollegeBasketballData's adjusted efficiency ratings (documented API, free key) drive opponent rank, featured-game ranking and top-25 movers. Torvik stays the source for player season stats and game logs, and the email still links teams and players to their Torvik pages.

## Considered options

- AP poll via ESPN rankings. Rejected because the poll is released Monday afternoon, after the 07:00 send, so movers would always be a week stale.
- CBBD for everything. Rejected because it gives up Torvik's player files, which answered without a key.

## Consequences

- If a Torvik player file ever starts returning the challenge, that section degrades to the one-line source-failure notice. The fallback is CBBD player stats, never a challenge solver.
- "Rank" in the email means CBBD net-rating rank, and the email labels it that way.
