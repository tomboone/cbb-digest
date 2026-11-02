"""The `Digest` assembled from the stored week, a failed source's banner and omitted sections,
and the `compute` command (spec §5, §9.1, §9.3).

The fixture week is the one `test_load` runs: season 2026, the week of 2026-11-02, whose ESPN day
is Tuesday 2026-11-03. Torvik's 2026 game logs end in April, so nobody played last week and the
watchlist section is empty; this week's games, the freshmen and the baseline movers line are not.
"""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, update
from syrupy.assertion import SnapshotAssertion
from typer.testing import CliRunner

from cbb_digest import db, watchlist
from cbb_digest.cli import app
from cbb_digest.compute.build import BANNER, Source, build_digest, subject_for, watchlist_url
from cbb_digest.compute.models import Digest
from cbb_digest.runs import ensure_run
from cbb_digest.schema import watchlist_resolution
from cbb_digest.sources import http
from cbb_digest.watchlist import Watchlist, load_watchlist
from cbb_digest.weeks import windows_for
from tests.test_load import sources as sources

MONDAY = date(2026, 11, 2)
RUN_ID = "2026-W45"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SHA = "0123456789abcdef0123456789abcdef01234567"

WATCHLIST = """\
season: 2026
players:
  - {name: Bennett Stirtz, team: Iowa, tier: 1, class: 2026}
  - {name: Nate Ament, team: Tennessee, tier: 1, class: 2026}
  - {name: Otega Oweh, team: Duke, tier: 2, class: 2026}
  - {name: Cameron Boozer, team: Duke, tier: 2, class: 2026}
  - {name: Nobody Real, team: Iowa, tier: 3, class: 2026}
"""

runner = CliRunner()


def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CBBD_API_KEY", "test-key")
    monkeypatch.setenv("RECIPIENTS", "tom@example.com")
    path = tmp_path / "watchlist.yaml"
    path.write_text(WATCHLIST, encoding="utf-8")
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)
    monkeypatch.setattr(http.time, "sleep", _no_sleep)
    return data_dir


@pytest.fixture
def week(env: Path, sources: object) -> Iterator[Engine]:
    """The fixture week fetched, loaded and resolved, as `compute` finds it."""
    for command in ("fetch", "load", "resolve"):
        result = runner.invoke(app, [command, "--week", MONDAY.isoformat()])
        assert result.exit_code == 0, (command, result.output)
    engine = db.open_database(env)
    yield engine
    engine.dispose()


def _build(connection: Connection, *failed: Source) -> Digest:
    return build_digest(
        connection, load_watchlist(), MONDAY, failed=set(failed), now=NOW, image_sha=SHA
    )


def test_the_fixture_week_digest(week: Engine, snapshot: SnapshotAssertion) -> None:
    with week.begin() as connection:
        digest = _build(connection)

    assert digest.model_dump(mode="json") == snapshot


def test_the_fixture_week_has_every_section_and_its_resolution_alerts(week: Engine) -> None:
    with week.begin() as connection:
        digest = _build(connection)

    assert digest.banner == []
    assert digest.featured is not None and digest.featured.rows
    assert digest.watchlist is not None
    assert digest.watchlist.alerts == []
    assert digest.freshmen is not None and digest.freshmen.rows
    assert digest.movers is not None and digest.movers.baseline
    assert [(alert.kind, alert.name) for alert in digest.alerts] == [
        ("team_changed", "Otega Oweh"),
        ("unresolved", "Nobody Real"),
    ]
    assert digest.footer.run_id == RUN_ID
    assert digest.footer.data_as_of == date(2026, 11, 1)
    assert digest.footer.run_ts_et.isoformat() == "2026-11-02T07:00:00-05:00"


def test_with_espn_failed_featured_games_are_omitted_with_one_banner_line(week: Engine) -> None:
    with week.begin() as connection:
        digest = _build(connection, "espn")

    assert digest.featured is None
    assert digest.banner == ["ESPN schedule unavailable this week — featured games omitted."]
    assert digest.watchlist is not None
    assert digest.freshmen is not None
    assert digest.movers is not None


def test_with_torvik_failed_the_watchlist_and_freshmen_are_omitted(week: Engine) -> None:
    with week.begin() as connection:
        digest = _build(connection, "torvik")

    assert digest.watchlist is None
    assert digest.freshmen is None
    assert digest.banner == [BANNER["torvik"]]
    assert "last successful run" in digest.banner[0]
    # Featured games still use the stored resolutions.
    assert digest.featured is not None and digest.featured.rows
    assert digest.movers is not None


def test_with_cbbd_failed_movers_and_freshmen_are_omitted_and_teams_are_unranked(
    week: Engine,
) -> None:
    with week.begin() as connection:
        ranked = _build(connection)
        digest = _build(connection, "cbbd")

    assert digest.movers is None
    assert digest.freshmen is None
    assert digest.banner == [BANNER["cbbd"]]
    assert ranked.featured is not None
    assert any(row.home.rank is not None for row in ranked.featured.rows)
    assert digest.featured is not None
    assert all(side.rank is None for row in digest.featured.rows for side in (row.home, row.away))


def test_every_source_failed_gives_one_line_each_in_section_order(week: Engine) -> None:
    with week.begin() as connection:
        digest = _build(connection, "cbbd", "torvik", "espn")

    assert digest.banner == [BANNER["espn"], BANNER["torvik"], BANNER["cbbd"]]
    assert (digest.featured, digest.watchlist, digest.freshmen, digest.movers) == (
        None,
        None,
        None,
        None,
    )


def test_a_second_run_lists_only_the_alerts_it_raised(week: Engine) -> None:
    with week.begin() as connection:
        # As if the week before had raised Oweh's alert, and this run only Nobody Real's.
        earlier = MONDAY - timedelta(days=7)
        ensure_run(
            connection, run_id="2026-W44", season=2026, windows=windows_for(earlier), now=NOW
        )
        connection.execute(
            update(watchlist_resolution)
            .where(watchlist_resolution.c.alert_kind == "team_changed")
            .values(changed_run_id="2026-W44")
        )
        digest = _build(connection)

    assert [alert.name for alert in digest.alerts] == ["Nobody Real"]


def test_a_week_that_was_never_loaded_is_a_lookup_error(env: Path) -> None:
    engine = db.open_database(env)
    try:
        with engine.begin() as connection, pytest.raises(LookupError, match="load"):
            build_digest(
                connection,
                Watchlist.model_validate({"season": 2026, "players": []}),
                MONDAY,
                failed=(),
                now=NOW,
                image_sha=None,
            )
    finally:
        engine.dispose()


def test_the_subject_names_the_runs_monday() -> None:
    assert subject_for(date(2026, 11, 9)) == "CBB digest — week of Nov 9"
    assert subject_for(date(2027, 1, 11)) == "CBB digest — week of Jan 11"


def test_the_watchlist_link_is_at_the_images_commit_or_on_main() -> None:
    assert watchlist_url(SHA) == f"https://github.com/tomboone/cbb-digest/blob/{SHA}/watchlist.yaml"
    assert watchlist_url(None) == "https://github.com/tomboone/cbb-digest/blob/main/watchlist.yaml"


# --- the command ---


def test_compute_writes_the_digest_json(week: Engine, env: Path) -> None:
    result = runner.invoke(app, ["compute", "--week", "2026-11-04"])

    assert result.exit_code == 0, result.output
    assert f"week of 2026-11-02, run {RUN_ID}" in result.output
    assert "banner:" not in result.output
    assert "movers: 0 rows" in result.output
    digest = Digest.model_validate_json((env / "out" / "digest.json").read_text(encoding="utf-8"))
    assert digest.run_id == RUN_ID
    assert digest.subject == "CBB digest — week of Nov 2"
    assert digest.footer.image_sha is None


def test_compute_treats_a_source_missing_from_the_cache_as_failed(week: Engine, env: Path) -> None:
    (env / "cache" / "espn" / "scoreboard-20261104").unlink()

    result = runner.invoke(app, ["compute", "--week", "2026-11-02"])

    assert result.exit_code == 0, result.output
    assert f"banner: {BANNER['espn']}" in result.output
    assert "featured: omitted" in result.output
    written = json.loads((env / "out" / "digest.json").read_text(encoding="utf-8"))
    assert written["featured"] is None
    assert written["banner"] == [BANNER["espn"]]


def test_compute_before_load_exits_1(env: Path) -> None:
    result = runner.invoke(app, ["compute", "--week", "2026-11-02"])

    assert result.exit_code == 1
    assert "load" in result.output
    assert not (env / "out" / "digest.json").exists()
