"""Torvik's player game logs, `{season}_all_advgames.json.gz` (spec §6).

A JSON list of 53-element lists, one per player per D-I game **played**: a player who did not play
has no row. Names follow dbcjason/NCAACards' `fetch_bt_advgames_by_year.py`; the full map is in
`tests/fixtures/README.md`. Index 8 is minutes played, and the box counts are per game. Only the
fields the digest reads are kept.
"""

import json
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from cbb_digest.sources.models.torvik_base import ShapeError, pick

FIELD_COUNT = 53

COLUMNS = {
    "game_date": 0,
    "opponent": 5,
    "min": 8,
    "fg2m": 23,
    "fg2a": 24,
    "fg3m": 25,
    "fg3a": 26,
    "fta": 28,
    "pts": 33,
    "orb": 34,
    "drb": 35,
    "ast": 36,
    "team": 47,
    "pid": 51,
}


class TorvikPlayerGame(BaseModel):
    model_config = ConfigDict(frozen=True)

    pid: int
    game_date: date
    team: str
    # Torvik's team name, as `team` is.
    opponent: str
    min: float
    pts: int
    orb: int
    drb: int
    ast: int
    fg2m: int
    fg2a: int
    fg3m: int
    fg3a: int
    fta: int

    @field_validator("game_date", mode="before")
    @classmethod
    def _parse_numdate(cls, value: object) -> object:
        # `YYYYMMDD`, which pydantic would otherwise read as a Unix timestamp.
        if isinstance(value, str):
            return datetime.strptime(value, "%Y%m%d").date()
        return value

    @property
    def reb(self) -> int:
        return self.orb + self.drb

    @property
    def fgm(self) -> int:
        return self.fg2m + self.fg3m

    @property
    def fga(self) -> int:
        return self.fg2a + self.fg3a


def parse_player_games(body: bytes) -> list[TorvikPlayerGame]:
    rows: object = json.loads(body)
    if not isinstance(rows, list):
        raise ShapeError(f"shape: body is {type(rows).__name__}, expected a list of rows")
    items: list[object] = rows  # pyright: ignore[reportUnknownVariableType]
    return [
        TorvikPlayerGame.model_validate(pick(row, COLUMNS, FIELD_COUNT, number))
        for number, row in enumerate(items, start=1)
    ]
