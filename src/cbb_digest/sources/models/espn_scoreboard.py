"""ESPN's men's college basketball scoreboard, one US-local day per request (spec §6, §8.3).

Follows nba-today's `espn_base` conventions: camelCase aliases, frozen models, and only the
fields the digest reads, everything else ignored. Optional fields (venue, status, team name,
broadcasts) default, so one game missing one never fails the day. A game with no id, no start,
no team id, or no home and away team fails the whole day instead: the featured games cannot rank
or map a game without them, and a shape change must fail loudly, never as a partial parse.

Each event becomes one `EspnGame`. Team ids stay ESPN's strings, the form CBBD's `sourceId`
(the crosswalk's `espn_id`) also uses. A team's name is its `location` (`Tennessee`), the form
the fixtures share with CBBD's `school` and Torvik. `events[].date` is UTC; the request's
`dates` names the US-local day, so most evening games carry the next day's UTC date.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _Team(_Model):
    id: str
    location: str | None = None


class _Competitor(_Model):
    home_away: str | None = None
    team: _Team


class _Venue(_Model):
    full_name: str | None = None


class _Broadcast(_Model):
    names: list[str] = []


class _Competition(_Model):
    venue: _Venue | None = None
    broadcasts: list[_Broadcast] = []
    competitors: list[_Competitor] = []


class _StatusType(_Model):
    name: str | None = None


class _Status(_Model):
    type: _StatusType | None = None


class _Event(_Model):
    id: str
    date: AwareDatetime
    status: _Status | None = None
    competitions: list[_Competition] = []


class _Scoreboard(_Model):
    # Optional only so its absence reads as a shape failure rather than a validation error.
    events: list[_Event] | None = None


@dataclass(frozen=True, slots=True)
class EspnTeam:
    espn_id: str
    name: str | None


@dataclass(frozen=True, slots=True)
class EspnGame:
    espn_id: str
    start: datetime
    home: EspnTeam
    away: EspnTeam
    venue: str | None
    # Every `broadcasts[].names` entry, deduplicated, in ESPN's order; `[]` when none is listed.
    networks: list[str]
    # ESPN's status name, e.g. `STATUS_SCHEDULED`.
    status: str | None


def parse_scoreboard(body: bytes) -> list[EspnGame]:
    scoreboard = _Scoreboard.model_validate_json(body)
    if scoreboard.events is None:
        raise ValueError("shape: scoreboard has no events")
    return [_game(event) for event in scoreboard.events]


def _game(event: _Event) -> EspnGame:
    if not event.competitions:
        raise ValueError(f"shape: event {event.id} has no competition")
    competition = event.competitions[0]
    sides = {competitor.home_away: competitor.team for competitor in competition.competitors}
    home, away = sides.get("home"), sides.get("away")
    if home is None or away is None:
        raise ValueError(f"shape: event {event.id} lacks a home or an away team")
    status_type = event.status.type if event.status is not None else None
    return EspnGame(
        espn_id=event.id,
        start=event.date.astimezone(UTC),
        home=EspnTeam(espn_id=home.id, name=home.location),
        away=EspnTeam(espn_id=away.id, name=away.location),
        venue=competition.venue.full_name if competition.venue is not None else None,
        networks=list(
            dict.fromkeys(name for broadcast in competition.broadcasts for name in broadcast.names)
        ),
        status=status_type.name if status_type is not None else None,
    )
