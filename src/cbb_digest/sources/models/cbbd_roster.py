"""CBBD's rosters for a season, `GET /teams/roster?season=<season>` (spec §6, §11).

One request returns every team CBBD knows, D-I or not; only D-I teams carry players. The roster is
flattened to one row per player, so a team with no players contributes no rows. Reset mode matches
watchlist players against these rows by name within a team; the class it reports comes from the
watchlist, not from CBBD, so nothing else about the player is kept.
"""

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Player(BaseModel):
    name: str


class _Team(BaseModel):
    team_id: int = Field(validation_alias="teamId")
    players: list[_Player]


class CbbdRosterPlayer(BaseModel):
    model_config = ConfigDict(frozen=True)

    team_id: int
    name: str


_TEAMS = TypeAdapter(list[_Team])


def parse_rosters(body: bytes) -> list[CbbdRosterPlayer]:
    return [
        CbbdRosterPlayer(team_id=team.team_id, name=player.name)
        for team in _TEAMS.validate_json(body)
        for player in team.players
    ]
