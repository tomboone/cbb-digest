"""The digest → the email: subject, MJML-compiled HTML, and a plain-text alternative (spec §5).

Pure: Jinja2 renders the MJML and text templates from the `Digest` alone, and `mjml` (the Python
port of the compiler, nba-today ADR-0003) turns the MJML into mail-client HTML. No I/O beyond the
packaged templates, and nothing in the email is fetched by the reader's client: no images, no
tracking pixels.

Every team and player name links to its Torvik page (§5). These are links for the reader's
browser only; the pipeline never fetches them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

import mjml
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from cbb_digest.compute.models import Digest, Mover
from cbb_digest.resolve import AlertKind
from cbb_digest.weeks import EASTERN

TORVIK = "https://barttorvik.com"
NO_VALUE = "—"
EN_DASH = "\u2013"
ALERT_LABELS: dict[AlertKind, str] = {
    "unresolved": "Unresolved",
    "ambiguous": "Ambiguous",
    "team_changed": "Team changed",
    "fuzzy_match": "Fuzzy match",
    "unmapped_team": "Unmapped team",
    "inactive": "Inactive",
}
MOVERS_MORE_LABEL = "full ratings table (Torvik's; ranks differ slightly)"
BASELINE_LINE = "Baseline snapshot taken — movers start next week."


@dataclass(frozen=True, slots=True)
class Rendered:
    subject: str
    html: str
    text: str


def render(digest: Digest) -> Rendered:
    """The digest's subject, HTML, and text alternative.

    Raises `ValueError` when the MJML does not compile.
    """
    links = _Links(digest.season, digest.torvik_teams)
    monday = digest.week_of
    context = {
        "digest": digest,
        "subject": digest.subject,
        "player_url": links.player,
        "team_url": links.team,
        "players_url": players_url(digest.season),
        "ratings_url": ratings_url(digest.season),
        "movers_more_label": MOVERS_MORE_LABEL,
        "baseline_line": BASELINE_LINE,
        "last_week": week_span(monday - timedelta(days=7), monday - timedelta(days=1)),
        "this_week": week_span(monday, monday + timedelta(days=6)),
    }
    source = _ENV.get_template("digest.mjml.j2").render(context)
    return Rendered(
        subject=digest.subject,
        html=mjml.mjml2html(source, disable_comments=True),
        text=_ENV.get_template("digest.txt.j2").render(context),
    )


def player_url(season: int, name: str, torvik_team: str) -> str:
    """A player's Torvik page: `…/playerstat.php?link=y&year=2027&p=Bennett%20Stirtz&t=Iowa`."""
    query = f"link=y&year={season}&p={_encode(name)}&t={_encode(torvik_team)}"
    return f"{TORVIK}/playerstat.php?{query}"


def team_url(season: int, torvik_team: str) -> str:
    """A team's Torvik page: `…/team.php?team=Iowa%20St.&year=2027`."""
    return f"{TORVIK}/team.php?team={_encode(torvik_team)}&year={season}"


def players_url(season: int) -> str:
    """Torvik's player list for the season, where the watchlist's `+N more` points."""
    return f"{TORVIK}/playerstat.php?link=y&year={season}"


def ratings_url(season: int) -> str:
    """Torvik's team ratings table for the season, where the movers' `+N more` points."""
    return f"{TORVIK}/trank.php?year={season}"


def one(value: float) -> str:
    """A per-game stat to one decimal: `28.7`."""
    return f"{value:.1f}"


def percent(value: float | None) -> str:
    """A 0-1 rate as a whole-number percentage: `0.584` → `58%`; `—` when there is none."""
    return NO_VALUE if value is None else f"{value * 100:.0f}%"


def rank(value: int | None) -> str:
    """`#12`, or empty for an unranked team."""
    return "" if value is None else f"#{value}"


def tip(when: datetime) -> str:
    """`7:00 PM ET`."""
    local = when.astimezone(EASTERN)
    return f"{local:%I:%M %p}".removeprefix("0") + " ET"


def game_day(when: datetime) -> str:
    """`Tue Nov 3`, in Eastern."""
    local = when.astimezone(EASTERN)
    return f"{local:%a %b} {local.day}"


def stamp(when: datetime) -> str:
    """`Nov 2, 7:00 AM ET`."""
    local = when.astimezone(EASTERN)
    return f"{local:%b} {local.day}, {tip(local)}"


def long_date(day: date) -> str:
    """`Nov 1, 2026`."""
    return f"{day:%b} {day.day}, {day.year}"


def week_span(first: date, last: date) -> str:
    """`Oct 26 – Nov 1`, with an en dash."""  # noqa: RUF002
    return f"{first:%b} {first.day} {EN_DASH} {last:%b} {last.day}"


def change(mover: Mover) -> str:
    """What happened to a mover since last week's rank, which is the one thing its row does not
    already show: `up 4 (was #16)`, `entered (was #30)`, `left (was #22)`."""
    if mover.kind == "entered":
        return "entered" + ("" if mover.prev_rank is None else f" (was #{mover.prev_rank})")
    if mover.kind == "left":
        return "left" + ("" if mover.prev_rank is None else f" (was #{mover.prev_rank})")
    assert mover.change is not None and mover.prev_rank is not None
    direction = "up" if mover.change > 0 else "down"
    return f"{direction} {abs(mover.change)} (was #{mover.prev_rank})"


def alert_label(kind: AlertKind) -> str:
    return ALERT_LABELS[kind]


def tier_label(tier: int) -> str:
    """`T1`, the compact tier tag a row carries."""
    return f"T{tier}"


def short_sha(sha: str | None) -> str:
    """The image's commit to 7 characters, or `dev` when running from a checkout."""
    return "dev" if sha is None else sha[:7]


@dataclass(frozen=True, slots=True)
class _Links:
    """The digest's Torvik links, keyed by slug. A slug the digest has no Torvik name for, or no
    slug at all (a non-D-I opponent, §7.5), gets no link: `None`."""

    season: int
    torvik_teams: dict[str, str]

    def player(self, name: str, slug: str) -> str | None:
        torvik_team = self.torvik_teams.get(slug)
        return None if torvik_team is None else player_url(self.season, name, torvik_team)

    def team(self, slug: str | None) -> str | None:
        torvik_team = None if slug is None else self.torvik_teams.get(slug)
        return None if torvik_team is None else team_url(self.season, torvik_team)


def _encode(value: str) -> str:
    """A query value, percent-encoded with spaces as `%20` (`p=Bennett%20Stirtz`)."""
    return quote(value, safe="")


_ENV = Environment(
    loader=PackageLoader("cbb_digest.render", "templates"),
    autoescape=select_autoescape(enabled_extensions=("mjml.j2",), default_for_string=False),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_FILTERS: dict[str, Callable[..., object]] = {
    "one": one,
    "percent": percent,
    "rank": rank,
    "tip": tip,
    "game_day": game_day,
    "stamp": stamp,
    "long_date": long_date,
    "change": change,
    "alert_label": alert_label,
    "tier_label": tier_label,
    "short_sha": short_sha,
}
_ENV.filters.update(_FILTERS)
