"""The digest → subject, MJML-compiled HTML and plain text, and the `render` command (spec §5).

Three digests are rendered: the fixture week `test_build` computes, the same week with every source
failed, and a hand-built week that fills every section, so each row shape and `+N more` line is
seen. The HTML and text snapshots are one file each, so a reviewer can open the email.
"""

import json
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode
from typer.testing import CliRunner

from cbb_digest.cli import app
from cbb_digest.compute.build import Source, build_digest, subject_for, watchlist_url
from cbb_digest.compute.models import (
    Digest,
    FeaturedGame,
    FeaturedGamesSection,
    FeaturedPlayer,
    FeaturedTeam,
    Footer,
    FreshmanRow,
    FreshmenSection,
    Mover,
    MoversSection,
    WatchlistWeekRow,
    WatchlistWeekSection,
)
from cbb_digest.render.render import (
    change,
    game_day,
    one,
    percent,
    player_url,
    rank,
    render,
    stamp,
    team_url,
    tip,
)
from cbb_digest.resolve import Alert
from cbb_digest.watchlist import load_watchlist
from cbb_digest.weeks import EASTERN
from tests.test_build import MONDAY, NOW, SHA
from tests.test_build import env as env
from tests.test_build import week as week
from tests.test_load import sources as sources

ALL_SOURCES: tuple[Source, ...] = ("espn", "torvik", "cbbd")
TORVIK = "https://barttorvik.com/"

runner = CliRunner()


class HTMLSnapshot(SingleFileSnapshotExtension):
    """One browsable `.html` file per snapshot, so a reviewer can open the rendered email."""

    file_extension = "html"
    _write_mode = WriteMode.TEXT


class TextSnapshot(SingleFileSnapshotExtension):
    file_extension = "txt"
    _write_mode = WriteMode.TEXT


class _Anchors(HTMLParser):
    """Every `<a>` in a document, as (text, href), and every text node outside one."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self.plain: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
        else:
            self.plain.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anchors.append(("".join(self._text), self._href))
            self._href = None


def _anchors(html: str) -> list[tuple[str, str]]:
    parser = _Anchors()
    parser.feed(html)
    return parser.anchors


def _unlinked_text(html: str) -> str:
    """The text outside links, up to the alerts section. Alerts name watchlist entries as written
    in `watchlist.yaml`, which have no Torvik page of their own."""
    parser = _Anchors()
    parser.feed(html)
    return "".join(parser.plain).split("Watchlist alerts")[0]


def _computed(connection: Connection, *failed: Source) -> Digest:
    return build_digest(
        connection, load_watchlist(), MONDAY, failed=set(failed), now=NOW, image_sha=SHA
    )


def _start(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=EASTERN)


def _full() -> Digest:
    """A mid-season week with every section populated and every row shape present."""
    iowa = FeaturedTeam(slug="iowa", display_name="Iowa", rank=12)
    duke = FeaturedTeam(slug="duke", display_name="Duke", rank=3)
    iowa_state = FeaturedTeam(slug="iowa-state", display_name="Iowa State", rank=None)
    small = FeaturedTeam(slug=None, display_name="Small College", rank=None)
    stirtz = FeaturedPlayer(name="Bennett Stirtz", torvik_pid=1, tier=1, team_slug="iowa")
    boozer = FeaturedPlayer(name="Cameron Boozer", torvik_pid=2, tier=2, team_slug="duke")
    return Digest(
        run_id="2026-W47",
        season=2026,
        subject=subject_for(date(2026, 11, 16)),
        week_of=date(2026, 11, 16),
        torvik_teams={
            "duke": "Duke",
            "iowa": "Iowa",
            "iowa-state": "Iowa St.",
            "st-johns": "St. John's",
        },
        featured=FeaturedGamesSection(
            rows=[
                FeaturedGame(
                    espn_id="1",
                    start=_start(17, 19),
                    networks="ESPN/ESPN+",
                    venue="Cameron Indoor Stadium",
                    home=duke,
                    away=iowa,
                    players=[stirtz, boozer],
                ),
                FeaturedGame(
                    espn_id="2",
                    start=_start(21, 12),
                    networks="—",
                    venue=None,
                    home=iowa,
                    away=small,
                    players=[stirtz],
                ),
                FeaturedGame(
                    espn_id="3",
                    start=_start(22, 20),
                    networks="BTN",
                    venue=None,
                    home=iowa_state,
                    away=iowa,
                    players=[stirtz],
                ),
            ],
            more_count=2,
        ),
        watchlist=WatchlistWeekSection(
            rows=[
                WatchlistWeekRow(
                    name="Bennett Stirtz",
                    torvik_pid=1,
                    tier=1,
                    team_slug="iowa",
                    team_name="Iowa",
                    games=2,
                    minutes_per_game=34.5,
                    points_per_game=22.0,
                    rebounds_per_game=4.5,
                    assists_per_game=6.25,
                    true_shooting=0.6149,
                    threes_attempted_per_game=7.0,
                    notable="Season high: 31 pts",
                ),
                WatchlistWeekRow(
                    name="Cameron Boozer",
                    torvik_pid=2,
                    tier=2,
                    team_slug="duke",
                    team_name="Duke",
                    games=0,
                    minutes_per_game=0.0,
                    points_per_game=0.0,
                    rebounds_per_game=0.0,
                    assists_per_game=0.0,
                    true_shooting=None,
                    threes_attempted_per_game=0.0,
                    notable="DNP — 2nd straight week",
                ),
            ],
            more_count=3,
        ),
        freshmen=FreshmenSection(
            rows=[
                FreshmanRow(
                    name="Zuby Ejiofor",
                    torvik_pid=3,
                    team_slug="st-johns",
                    team_name="St. John's",
                    team_rank=18,
                    games=4,
                    minutes_per_game=30.25,
                    points_per_game=14.0,
                    rebounds_per_game=8.75,
                    assists_per_game=1.5,
                    true_shooting=0.577,
                )
            ]
        ),
        movers=MoversSection(
            rows=[
                Mover(
                    slug="iowa",
                    display_name="Iowa",
                    rank=12,
                    prev_rank=18,
                    change=6,
                    kind="moved",
                ),
                Mover(
                    slug="duke", display_name="Duke", rank=3, prev_rank=7, change=4, kind="moved"
                ),
                Mover(
                    slug="st-johns",
                    display_name="St. John's",
                    rank=20,
                    prev_rank=31,
                    change=11,
                    kind="entered",
                ),
                Mover(
                    slug="iowa-state",
                    display_name="Iowa State",
                    rank=29,
                    prev_rank=22,
                    change=-7,
                    kind="left",
                ),
            ],
            more_count=1,
        ),
        alerts=[
            Alert(kind="team_changed", name="Otega Oweh", detail="was Duke, now Kentucky"),
            Alert(kind="inactive", name="Cameron Boozer", detail="DNP 2 straight weeks"),
        ],
        footer=Footer(
            run_ts_et=datetime(2026, 11, 16, 7, 0, 4, tzinfo=EASTERN),
            data_as_of=date(2026, 11, 15),
            run_id="2026-W47",
            image_sha=SHA,
            watchlist_url=watchlist_url(SHA),
        ),
    )


@pytest.fixture
def computed(week: Engine) -> Digest:
    with week.begin() as connection:
        return _computed(connection)


@pytest.fixture
def all_failed(week: Engine) -> Digest:
    with week.begin() as connection:
        return _computed(connection, *ALL_SOURCES)


@pytest.fixture(params=["computed", "all_failed", "full"])
def digest(request: pytest.FixtureRequest) -> Digest:
    if request.param == "full":
        return _full()
    return request.getfixturevalue(request.param)


def test_html_matches_the_snapshot(digest: Digest, snapshot: SnapshotAssertion) -> None:
    assert render(digest).html == snapshot(extension_class=HTMLSnapshot)


def test_text_matches_the_snapshot(digest: Digest, snapshot: SnapshotAssertion) -> None:
    assert render(digest).text == snapshot(extension_class=TextSnapshot)


def test_the_subject_is_the_digests(digest: Digest) -> None:
    assert render(digest).subject == digest.subject


def test_the_html_has_no_images(digest: Digest) -> None:
    assert "<img" not in render(digest).html.lower()


def test_every_player_and_team_name_links_to_torvik() -> None:
    digest = _full()
    html = render(digest).html
    anchors = _anchors(html)
    linked = {text for text, href in anchors if href.startswith(TORVIK)}

    assert digest.featured is not None and digest.watchlist is not None
    assert digest.freshmen is not None and digest.movers is not None
    names: set[str] = {
        *(p.name for g in digest.featured.rows for p in g.players),
        *(
            t.display_name
            for g in digest.featured.rows
            for t in (g.home, g.away)
            if t.slug and t.display_name
        ),
        *(r.name for r in digest.watchlist.rows),
        *(r.team_name for r in digest.watchlist.rows),
        *(r.name for r in digest.freshmen.rows),
        *(r.team_name for r in digest.freshmen.rows),
        *(r.display_name for r in digest.movers.rows),
    }
    assert names - linked == set()
    # Every occurrence, not just one: no name is left as plain text anywhere in the sections.
    unlinked = _unlinked_text(html)
    assert [name for name in names if name in unlinked] == []


def test_the_fixture_weeks_names_link_to_torvik(computed: Digest) -> None:
    anchors = _anchors(render(computed).html)
    linked = {text for text, href in anchors if href.startswith(TORVIK)}

    assert computed.featured is not None and computed.freshmen is not None
    names = {
        *(p.name for g in computed.featured.rows for p in g.players),
        *(t.display_name for g in computed.featured.rows for t in (g.home, g.away) if t.slug),
        *(r.name for r in computed.freshmen.rows),
        *(r.team_name for r in computed.freshmen.rows),
    }
    assert names and names - linked == set()


def test_a_team_the_crosswalk_does_not_know_is_not_linked() -> None:
    html = render(_full()).html

    assert "Small College" in html
    assert all(text != "Small College" for text, _ in _anchors(html))


def test_links_use_the_spec_forms_url_encoded() -> None:
    assert player_url(2027, "Bennett Stirtz", "Iowa") == (
        "https://barttorvik.com/playerstat.php?link=y&year=2027&p=Bennett%20Stirtz&t=Iowa"
    )
    assert team_url(2027, "St. John's") == (
        "https://barttorvik.com/team.php?team=St.%20John%27s&year=2027"
    )
    assert 'href="https://barttorvik.com/team.php?team=Iowa%20St.&amp;year=2026"' in (
        render(_full()).html
    )


def test_the_movers_more_link_names_torviks_table() -> None:
    html = render(_full()).html

    assert (
        "full ratings table (Torvik's; ranks differ slightly)",
        ("https://barttorvik.com/trank.php?year=2026"),
    ) in _anchors(html)


def test_with_every_source_failed_only_the_banner_alerts_and_footer_remain(
    all_failed: Digest,
) -> None:
    rendered = render(all_failed)

    for line in all_failed.banner:
        assert line in rendered.text
    assert len(all_failed.banner) == len(ALL_SOURCES)
    for heading in ("Featured games", "Watchlist last week", "Freshmen to watch", "Top-25 movers"):
        assert heading not in rendered.html
        assert heading.upper() not in rendered.text
    assert "Watchlist alerts" in rendered.html
    assert all_failed.footer.run_id in rendered.html


def test_the_baseline_week_prints_the_baseline_line(computed: Digest) -> None:
    rendered = render(computed)

    assert "Baseline snapshot taken — movers start next week." in rendered.html
    assert "Baseline snapshot taken — movers start next week." in rendered.text


@pytest.mark.parametrize(
    ("value", "expected"), [(28.66, "28.7"), (0.0, "0.0"), (6.25, "6.2"), (10.0, "10.0")]
)
def test_one_is_one_decimal(value: float, expected: str) -> None:
    assert one(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(0.584, "58%"), (0.5, "50%"), (None, "—")])
def test_percent_is_a_whole_number(value: float | None, expected: str) -> None:
    assert percent(value) == expected


def test_rank_is_hash_prefixed_and_empty_when_unranked() -> None:
    assert rank(12) == "#12"
    assert rank(None) == ""


def test_times_read_in_eastern() -> None:
    evening = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)

    assert tip(evening) == "7:00 PM ET"
    assert game_day(evening) == "Tue Nov 3"
    assert stamp(evening) == "Nov 3, 7:00 PM ET"


@pytest.mark.parametrize(
    ("mover", "expected"),
    [
        (
            Mover(slug="a", display_name="A", rank=4, prev_rank=9, change=5, kind="moved"),
            "up 5 (was #9)",
        ),
        (
            Mover(slug="a", display_name="A", rank=14, prev_rank=9, change=-5, kind="moved"),
            "down 5 (was #9)",
        ),
        (
            Mover(slug="a", display_name="A", rank=20, prev_rank=40, change=20, kind="entered"),
            "entered (was #40)",
        ),
        (
            Mover(slug="a", display_name="A", rank=21, prev_rank=None, change=None, kind="entered"),
            "entered",
        ),
        (
            Mover(slug="a", display_name="A", rank=30, prev_rank=24, change=-6, kind="left"),
            "left (was #24)",
        ),
        (
            Mover(slug="a", display_name="A", rank=None, prev_rank=24, change=None, kind="left"),
            "left (was #24)",
        ),
    ],
)
def test_change(mover: Mover, expected: str) -> None:
    assert change(mover) == expected


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def test_render_writes_the_html_and_text_of_the_computed_week(week: Engine, env: Path) -> None:
    code, output = _invoke("compute", "--week", MONDAY.isoformat())
    assert code == 0, output

    code, output = _invoke("render", "--week", MONDAY.isoformat(), "--dry-run")

    assert code == 0, output
    out = env / "out"
    digest = Digest.model_validate(json.loads((out / "digest.json").read_text(encoding="utf-8")))
    rendered = render(digest)
    assert (out / "digest.html").read_text(encoding="utf-8") == rendered.html
    assert (out / "digest.txt").read_text(encoding="utf-8") == rendered.text
    assert f"subject: {digest.subject}" in output


def test_render_without_a_computed_digest_exits_1(env: Path) -> None:
    code, output = _invoke("render", "--week", MONDAY.isoformat())

    assert code == 1
    assert "run `cbb-digest compute` first" in output
    assert not (env / "out" / "digest.html").exists()


def test_render_refuses_another_weeks_digest(week: Engine, env: Path) -> None:
    code, output = _invoke("compute", "--week", MONDAY.isoformat())
    assert code == 0, output

    code, output = _invoke("render", "--week", "2026-11-09")

    assert code == 1
    assert "is run 2026-W45, not 2026-W46" in output
    assert not (env / "out" / "digest.html").exists()
