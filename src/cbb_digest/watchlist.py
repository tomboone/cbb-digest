"""The watchlist: `watchlist.yaml`, its validation, and name normalisation (spec §7.1, §7.2).

The watchlist is the only input the reader supplies, edited by hand and by PR; the pipeline never
writes it. A mistake in it must stop the run before anything is fetched, never surface as a wrong
email, so `load_watchlist` checks the whole file and reports every error at once in a
`WatchlistError`, rather than stopping at the first.

The season the pipeline runs for is the file's `season` (spec §9.5).
"""

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

# The repo root in a checkout, and `/app` in the image, which copies the file there.
WATCHLIST_PATH = Path(__file__).resolve().parents[2] / "watchlist.yaml"

# Name suffixes dropped from the end of a normalised name (§7.2).
SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
# The team part of the watchlist key when the entry states no team (§7.3).
NO_TEAM = "—"
# Prefixes the stated team in the key of an entry whose team does not map (§7.3).
UNMAPPED_MARK = "?"

# `\u2019` is the right single quotation mark, a typographic apostrophe.
_REMOVED_CHARACTERS = re.compile("[.'\u2019]")
_SEASON = TypeAdapter(Annotated[int, Strict()])
_FILE_KEYS = ("season", "players")


def normalise_name(name: str) -> str:
    """Casefold, strip accents, drop `.`, `'` and its typographic form U+2019, collapse
    whitespace, then drop trailing `jr`/`sr`/`ii`/`iii`/`iv` (§7.2).
    `Dāvis O'Neal III` → `davis oneal`.

    A suffix is never the whole name: the first word is always kept.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    words = _REMOVED_CHARACTERS.sub("", unaccented).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


class WatchlistPlayer(BaseModel):
    """One entry on the watchlist. Unknown fields are typos, and refused.

    Validate with `context={"season": <season>}` to check `class` against the season; without a
    season in the context that check is skipped.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    # As of when the entry was added; a blank team is no team.
    team: str | None = None
    tier: Literal[1, 2, 3]
    # The expected draft year. `class` is a keyword, hence the alias.
    class_: int = Field(alias="class")
    note: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str) -> str:
        if not normalise_name(value):
            raise PydanticCustomError("blank_name", "must not be blank")
        return value

    @field_validator("team", mode="before")
    @classmethod
    def _blank_team_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("class_")
    @classmethod
    def _class_is_not_before_season(cls, value: int, info: ValidationInfo) -> int:
        context: object = info.context
        season = (
            cast(Mapping[str, object], context).get("season")
            if isinstance(context, Mapping)
            else None
        )
        if isinstance(season, int) and value < season:
            raise PydanticCustomError(
                "class_before_season",
                "{draft_class} is before season {season}",
                {"draft_class": value, "season": season},
            )
        return value


class Watchlist(BaseModel):
    """A validated watchlist. Build it with `load_watchlist`: the checks across entries (`class`
    against the season, duplicates) run there, not on direct construction."""

    model_config = ConfigDict(frozen=True)

    season: int
    players: tuple[WatchlistPlayer, ...]


class WatchlistError(ValueError):
    """`watchlist.yaml` is invalid; `errors` lists every problem found, one line each."""

    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        super().__init__(f"{path}: invalid watchlist\n" + "\n".join(f"  - {e}" for e in errors))


def watchlist_key(player: WatchlistPlayer, team_slug: str | None) -> str:
    """The player's key in `watchlist_resolution` (§7.3): the normalised name and the slug of
    the stated team, or `—` when the entry states no team. `tyran stokes|kansas`.

    `team_slug` is the crosswalk's slug for `player.team`, and `None` exactly when the entry
    states no team. A stated team the crosswalk cannot map has no key: keying it as `—` would
    merge it with a blank-team entry of the same name, so this raises `ValueError` instead.
    """
    if (player.team is None) != (team_slug is None):
        raise ValueError(
            f"team slug {team_slug!r} does not match the stated team {player.team!r} "
            f"of {player.name!r}"
        )
    return f"{normalise_name(player.name)}|{team_slug or NO_TEAM}"


def unmapped_watchlist_key(player: WatchlistPlayer) -> str:
    """The key of a player whose stated team the crosswalk cannot map: the normalised name and
    `?` + the team as written, casefolded. `tyran stokes|?kanzas`.

    A slug never starts with `?` and `—` is not one, so this key cannot collide with a mapped or
    a blank-team entry. Once the team maps, the entry gets its `watchlist_key` and a new row.
    """
    if player.team is None:
        raise ValueError(f"{player.name!r} states no team")
    return f"{normalise_name(player.name)}|{UNMAPPED_MARK}{player.team.strip().casefold()}"


def load_watchlist(path: Path | None = None) -> Watchlist:
    """The watchlist in `path` (default: the committed `watchlist.yaml`), validated.

    Raises `WatchlistError` listing every error in the file: a missing or invalid `season`, an
    invalid entry, a `class` before the season, and two entries with the same normalised name and
    team (compared without case or surrounding spaces).
    """
    path = path or WATCHLIST_PATH
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WatchlistError(path, [f"cannot read: {exc.strerror or exc}"]) from exc
    except yaml.YAMLError as exc:
        raise WatchlistError(path, [f"not valid YAML: {_first_line(str(exc))}"]) from exc
    if not isinstance(data, dict):
        raise WatchlistError(path, ["the file is not a mapping with `season` and `players`"])
    fields: dict[Any, Any] = data  # pyright: ignore[reportUnknownVariableType]

    errors = [f"{key}: Extra inputs are not permitted" for key in fields if key not in _FILE_KEYS]
    season: int | None = None
    if "season" not in fields:
        errors.append("season: Field required")
    else:
        try:
            season = _SEASON.validate_python(fields["season"])
        except ValidationError as exc:
            errors.extend(f"season: {error['msg']}" for error in exc.errors())

    players: list[WatchlistPlayer] = []
    raw_players = fields.get("players")
    if "players" not in fields:
        errors.append("players: Field required")
    elif not isinstance(raw_players, list):
        errors.append("players: Input should be a valid list")
    else:
        entries: list[object] = raw_players  # pyright: ignore[reportUnknownVariableType]
        players = _validate_players(entries, season, errors)

    if errors:
        raise WatchlistError(path, errors)
    assert season is not None
    return Watchlist(season=season, players=tuple(players))


def _validate_players(
    entries: list[object], season: int | None, errors: list[str]
) -> list[WatchlistPlayer]:
    """The valid entries; every invalid one, and every duplicate, is appended to `errors`."""
    players: list[WatchlistPlayer] = []
    seen: dict[tuple[str, str | None], int] = {}
    for index, entry in enumerate(entries):
        label = _label(index, entry)
        try:
            players.append(WatchlistPlayer.model_validate(entry, context={"season": season}))
        except ValidationError as exc:
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"])
                errors.append(
                    f"{label}: {location}: {error['msg']}"
                    if location
                    else f"{label}: {error['msg']}"
                )
        identity = _identity(entry)
        if identity is None:
            continue
        if identity in seen:
            errors.append(f"{label}: same normalised name and team as players[{seen[identity]}]")
        else:
            seen[identity] = index
    return players


def _label(index: int, entry: object) -> str:
    name = _field(entry, "name")
    if isinstance(name, str) and name.strip():
        return f"players[{index}] ({name.strip()})"
    return f"players[{index}]"


def _identity(entry: object) -> tuple[str, str | None] | None:
    """(normalised name, team) for the duplicate check, read from the raw entry so that an entry
    with some other error is still compared; `None` when it has no usable name or team."""
    name, team = _field(entry, "name"), _field(entry, "team")
    if not isinstance(name, str) or not normalise_name(name):
        return None
    if team is not None and not isinstance(team, str):
        return None
    return normalise_name(name), (team.strip().casefold() or None) if team is not None else None


def _field(entry: object, key: str) -> object:
    if isinstance(entry, dict):
        fields: dict[Any, Any] = entry  # pyright: ignore[reportUnknownVariableType]
        return fields.get(key)
    return None


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else "unknown error"
