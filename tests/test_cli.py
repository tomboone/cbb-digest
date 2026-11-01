import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cbb_digest import watchlist
from cbb_digest.cli import app

runner = CliRunner()

COMMANDS = [
    "fetch",
    "load",
    "resolve",
    "compute",
    "render",
    "send",
    "run",
    "serve",
    "reset",
    "health",
]
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    # Typer forces Rich styling under GITHUB_ACTIONS; strip it so word boundaries match.
    return ANSI_ESCAPE.sub("", output)


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = _plain(result.output)
    for command in COMMANDS:
        assert re.search(rf"\b{command}\b", output), command


@pytest.mark.parametrize(
    "args",
    [
        ["fetch"],
        ["load"],
        ["resolve"],
        ["compute"],
        ["render", "--dry-run"],
        ["send", "--force"],
        ["run", "--week", "2026-11-09", "--force", "--dry-run"],
        ["reset", "--season", "2028", "--send"],
    ],
)
def test_stub_exits_1_not_implemented(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert f"{args[0]}: not implemented" in result.output


def test_reset_requires_season() -> None:
    result = runner.invoke(app, ["reset"])

    assert result.exit_code == 2
    assert "--season" in _plain(result.output)


def test_run_rejects_a_malformed_week() -> None:
    result = runner.invoke(app, ["run", "--week", "Nov 9"])

    assert result.exit_code == 2


@pytest.mark.parametrize("args", [["resolve"], ["run", "--week", "2026-11-09"]])
def test_an_invalid_watchlist_stops_the_command_listing_every_error(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "season: 2027\n"
        "players:\n"
        "  - {name: Tyran Stokes, team: Kansas, tier: 4, class: 2027}\n"
        "  - {name: Darryn Peterson, team: Kansas, tier: 1, class: 2026}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)

    result = runner.invoke(app, args)

    assert result.exit_code == 2
    assert "invalid watchlist" in result.output
    assert "players[0] (Tyran Stokes): tier:" in result.output
    assert "players[1] (Darryn Peterson): class: 2026 is before season 2027" in result.output
    assert "not implemented" not in result.output
