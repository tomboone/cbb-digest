"""CBBD's adjusted efficiency ratings, `GET /ratings/adjusted?season=<season>` (spec §6).

One entry per D-I team, once games have been played; before that the endpoint answers `[]`, which
is a valid response meaning every team is unranked. These ratings drive rank, featured games and
movers, because Torvik's ratings endpoint is behind a browser check (ADR-0002).

**Rank** is the team's position by net rating, computed here by `rank_ratings` (spec §3). It is
not CBBD's own `rankings.net`, which gives tied teams the same rank and skips the next numbers
(61, 61, 63). Net rating is served to one decimal, so ties are common; here every team gets its own
position and tied teams keep CBBD's list order.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CbbdRating(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    team_id: int = Field(validation_alias="teamId")
    net: float = Field(validation_alias="netRating")
    adj_o: float = Field(validation_alias="offensiveRating")
    adj_d: float = Field(validation_alias="defensiveRating")


@dataclass(frozen=True, slots=True)
class RankedRating:
    rank: int
    rating: CbbdRating


_RATINGS = TypeAdapter(list[CbbdRating])


def parse_ratings(body: bytes) -> list[CbbdRating]:
    return _RATINGS.validate_json(body)


def rank_ratings(ratings: list[CbbdRating]) -> list[RankedRating]:
    """Ranks 1..N by net rating, highest first. Ties keep CBBD's order (the sort is stable)."""
    ordered = sorted(ratings, key=lambda rating: rating.net, reverse=True)
    return [RankedRating(rank, rating) for rank, rating in enumerate(ordered, start=1)]
