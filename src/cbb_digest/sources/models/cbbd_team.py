"""CBBD's D-I teams, `GET /teams?season=<season>` (spec §6).

One entry per D-I team. `sourceId` is the ESPN team id, which seeds the crosswalk's `espn_id`
(spec §7.5). Only the fields the crosswalk reads are kept; the rest (logos, venue, colours) are
ignored rather than validated.
"""

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class CbbdTeam(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: int
    school: str
    display_name: str = Field(validation_alias="displayName")
    # The ESPN team id, served as a string.
    espn_id: str = Field(validation_alias="sourceId")


_TEAMS = TypeAdapter(list[CbbdTeam])


def parse_teams(body: bytes) -> list[CbbdTeam]:
    return _TEAMS.validate_json(body)
