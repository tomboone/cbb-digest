"""The digest, field for field (spec §5).

Each section is its own model, so each builder can be tested on its own. `Digest` holds them, and
is the only thing the templates read; `compute.build` assembles it.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cbb_digest.resolve import Alert

type MoverKind = Literal["moved", "entered", "left"]
type Tier = Literal[1, 2, 3]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class FeaturedTeam(_Model):
    """One side of a featured game.

    `display_name` is CBBD's name for the team. A team the crosswalk does not know, such as a
    non-D-I opponent, has no `slug`, is shown under ESPN's name and gets no link (spec §7.5);
    `display_name` is `None` only if ESPN gave no name either. `rank` is this run's ratings
    rank, and `None` for a team missing from the snapshot or with no slug.
    """

    slug: str | None
    display_name: str | None
    rank: int | None


class FeaturedPlayer(_Model):
    """A resolved watchlist player on one of the two teams. `name` is Torvik's spelling."""

    name: str
    torvik_pid: int
    tier: Tier
    team_slug: str


class FeaturedGame(_Model):
    """One featured game (spec §8.3): `start` is aware Eastern, and `networks` is ESPN's
    broadcast names joined with `/`, or `—` when the game has none. `players` are sorted by tier,
    then name."""

    espn_id: str
    start: datetime
    networks: str
    venue: str | None
    home: FeaturedTeam
    away: FeaturedTeam
    players: list[FeaturedPlayer]


class FeaturedGamesSection(_Model):
    """Section 1: at most 5 featured games, and how many more qualified."""

    rows: list[FeaturedGame] = []
    more_count: int = 0


class Mover(_Model):
    """One team in the movers section (spec §8.6).

    `rank` is `None` for a team that left the top 25 and is missing from this week's snapshot,
    and `prev_rank` is `None` for one that entered and was missing from last week's. `change` is
    `prev_rank - rank`, positive for a team that moved up, and `None` when either rank is.
    """

    slug: str
    display_name: str
    rank: int | None
    prev_rank: int | None
    change: int | None
    kind: MoverKind


class MoversSection(_Model):
    """Section 4: at most 10 movers, and how many more qualified.

    `baseline` is true when the season has no earlier snapshot to compare with, and then `rows`
    is empty and the email prints the baseline line instead.
    """

    rows: list[Mover] = []
    more_count: int = 0
    baseline: bool = False


class WatchlistWeekRow(_Model):
    """One resolved watchlist player whose team played last week (spec §5, section 2).

    `name` is Torvik's spelling and `team_name` CBBD's. The per-game figures are over the games
    the player played last week, and are all 0 for a player who did not play (`games == 0`).
    `true_shooting` is a fraction (0.62 for `TS 62%`), and `None` when the player took no
    field-goal or free-throw attempts. `notable` is the one §8.4 flag, if any.
    """

    name: str
    torvik_pid: int
    tier: Tier
    team_slug: str
    team_name: str
    games: int
    minutes_per_game: float
    points_per_game: float
    rebounds_per_game: float
    assists_per_game: float
    true_shooting: float | None
    threes_attempted_per_game: float
    notable: str | None


class WatchlistWeekSection(_Model):
    """Section 2: at most 40 rows, and how many more qualified.

    `alerts` are the `inactive` alerts this run raised (§7.4); the email shows them in the alerts
    section, not here.
    """

    rows: list[WatchlistWeekRow] = []
    more_count: int = 0
    alerts: list[Alert] = []


class FreshmanRow(_Model):
    """One freshman to watch (spec §8.5), from Torvik's season-to-date totals.

    `name` is Torvik's spelling and `team_name` CBBD's; `team_rank` is this run's ratings rank.
    The per-game figures are season totals over `games`, and `true_shooting` is a fraction, `None`
    when the player took no field-goal or free-throw attempts.
    """

    name: str
    torvik_pid: int
    team_slug: str
    team_name: str
    team_rank: int
    games: int
    minutes_per_game: float
    points_per_game: float
    rebounds_per_game: float
    assists_per_game: float
    true_shooting: float | None


class FreshmenSection(_Model):
    """Section 3: at most 5 freshmen, with no `+N more`."""

    rows: list[FreshmanRow] = []


class Footer(_Model):
    """Section 6: where this digest came from.

    `run_ts_et` is when it was computed, aware Eastern. `data_as_of` is the last day of last week,
    the day the stats run through. `image_sha` is the commit the image was built from, and `None`
    when running from a checkout; `watchlist_url` points at `watchlist.yaml` at that commit, or on
    `main` without one.
    """

    run_ts_et: datetime
    data_as_of: date
    run_id: str
    image_sha: str | None
    watchlist_url: str


class Digest(_Model):
    """One week's digest, the only thing the templates may read (spec §5).

    `banner` holds one line per source that failed this run, and a section that depends on a
    failed source is `None`, so it is omitted from the email (§9.3). `alerts` are the alerts this
    run raised: resolution's, then `inactive` (§7.4). They are the only copy: the watchlist
    section's own `alerts` are moved here and left empty.
    """

    run_id: str
    kind: Literal["digest"] = "digest"
    subject: str
    week_of: date
    banner: list[str] = []
    featured: FeaturedGamesSection | None
    watchlist: WatchlistWeekSection | None
    freshmen: FreshmenSection | None
    movers: MoversSection | None
    alerts: list[Alert] = []
    footer: Footer
