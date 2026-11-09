"""Torvik's player season file, `getadvstats.php?year=<season>&csv=1` (spec §6).

A headerless CSV of 78 fields per row. Fields 0-66 are in hoopR's `R/torvik_player_stats.R` order;
Torvik appended fields 67-77, which are unnamed, on or before 2026-11-09 (NEU-2262). The full map is
in `tests/fixtures/README.md`. Only the fields the digest reads are kept.

Units differ by field, as Torvik serves them: the shooting makes and attempts are **season
totals**, while minutes, rebounds, assists and points are **per-game averages**. The names say
which. `season` is field 31 as served: before a season starts the file can hold last season's
rows, so the loader keeps only rows whose `season` matches the one it asked for.
"""

import csv
import io
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cbb_digest.sources.models.torvik_base import pick

FIELD_COUNT = 78

COLUMNS = {
    "name": 0,
    "team": 1,
    "games": 3,
    "fta": 14,
    "fg2m": 16,
    "fg2a": 17,
    "fg3m": 19,
    "fg3a": 20,
    "class_year": 25,
    "season": 31,
    "pid": 32,
    "min_per_game": 54,
    "reb_per_game": 59,
    "ast_per_game": 60,
    "pts_per_game": 63,
}

type ClassYear = Literal["Fr", "So", "Jr", "Sr"]


class TorvikPlayerSeason(BaseModel):
    model_config = ConfigDict(frozen=True)

    pid: int
    season: int
    name: str
    team: str
    # Blank in the file means unknown (`players.class_year` is nullable); any other value outside
    # `ClassYear` is a shape change and fails the source.
    class_year: ClassYear | None
    games: int
    # Per-game averages.
    min_per_game: float
    pts_per_game: float
    reb_per_game: float
    ast_per_game: float
    # Season totals.
    fg2m: int
    fg2a: int
    fg3m: int
    fg3a: int
    fta: int

    @field_validator("class_year", mode="before")
    @classmethod
    def _blank_class_is_unknown(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def fgm(self) -> int:
        return self.fg2m + self.fg3m

    @property
    def fga(self) -> int:
        return self.fg2a + self.fg3a


def parse_player_season(body: bytes) -> list[TorvikPlayerSeason]:
    rows = csv.reader(io.StringIO(body.decode("utf-8")))
    return [
        TorvikPlayerSeason.model_validate(pick(row, COLUMNS, FIELD_COUNT, number))
        for number, row in enumerate(rows, start=1)
        if row  # a blank line, not a row
    ]
