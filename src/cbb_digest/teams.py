"""The team crosswalk: one canonical team per D-I school, and each source's name for it (spec §7.5).

CBBD, Torvik and ESPN name teams differently (`Iowa State` / `Iowa St.` / ESPN id `66`), so every
join goes through a canonical team keyed by a slug. The canonical teams are seeded from CBBD's
D-I teams, the ratings source:

- the slug is the kebab-case of CBBD's `school` (`St. John's` → `st-johns`);
- `espn_id` is CBBD's `sourceId`, which is the ESPN team id, so ESPN needs no hand mapping;
- `torvik_name` is CBBD's `school`, unless `teams_crosswalk.yaml` overrides it.

The overrides are listed explicitly, one per team, and never derived by a rewrite rule such as
`State` → `St.`: a rule would silently mis-map the team it does not fit.

`espn_name` is not seeded. CBBD carries no ESPN name, and its `school` is not one (`New Orleans`
is `LSU New Orleans` on ESPN), so the column stays NULL until something that reads ESPN sets it.

A lookup returns `None` for a name or id the crosswalk does not know. The caller raises an
`unmapped_team` alert (§7.4); nothing here guesses. Storing the canonical teams in the `teams`
table is the loader's job (spec §9.1 `load`).
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from cbb_digest.sources.models.cbbd_team import CbbdTeam

# The repo root in a checkout, and `/app` in the image, which copies the file there.
CROSSWALK_PATH = Path(__file__).resolve().parents[2] / "teams_crosswalk.yaml"


@dataclass(frozen=True, slots=True)
class CanonicalTeam:
    """One row of `teams`."""

    slug: str
    cbbd_id: int
    cbbd_name: str
    torvik_name: str
    espn_id: str
    espn_name: str | None = None


class TeamOverride(BaseModel):
    """One team's hand mapping in `teams_crosswalk.yaml`. An unknown field is a typo, not data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    torvik_name: str


_OVERRIDES = TypeAdapter(dict[str, TeamOverride])


def slugify(name: str) -> str:
    """Kebab-case `name`: ASCII, lower case, apostrophes dropped, any other run of punctuation or
    spaces a single hyphen. `San José State` → `san-jose-state`, `Miami (OH)` → `miami-oh`."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # A curly apostrophe is already gone: the ASCII encode drops it.
    without_apostrophes = ascii_name.lower().replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", without_apostrophes).strip("-")


def load_overrides(path: Path = CROSSWALK_PATH) -> dict[str, TeamOverride]:
    """The overrides in `path`, keyed by slug. An empty file has none."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _OVERRIDES.validate_python(data or {})


def build_teams(
    cbbd_teams: Iterable[CbbdTeam], overrides: Mapping[str, TeamOverride]
) -> list[CanonicalTeam]:
    """The canonical teams, sorted by slug, seeded from CBBD and corrected by `overrides`.

    Raises `ValueError` when an override names a slug CBBD does not have, or when two teams end
    up with the same slug, CBBD id, Torvik name or ESPN id: either is a crosswalk that would join
    the wrong team, and is fixed in `teams_crosswalk.yaml`, never at run time.
    """
    teams: list[CanonicalTeam] = []
    for team in cbbd_teams:
        slug = slugify(team.school)
        override = overrides.get(slug)
        teams.append(
            CanonicalTeam(
                slug=slug,
                cbbd_id=team.id,
                cbbd_name=team.school,
                torvik_name=override.torvik_name if override is not None else team.school,
                espn_id=team.espn_id,
            )
        )
    unknown = sorted(set(overrides) - {team.slug for team in teams})
    if unknown:
        raise ValueError(f"teams_crosswalk.yaml overrides slugs CBBD does not have: {unknown}")
    for field in ("slug", "cbbd_id", "torvik_name", "espn_id"):
        counts = Counter(getattr(team, field) for team in teams)
        shared = sorted(str(value) for value, count in counts.items() if count > 1)
        if shared:
            raise ValueError(f"teams share a {field}: {shared}")
    return sorted(teams, key=lambda team: team.slug)


@dataclass(frozen=True, slots=True)
class Crosswalk:
    """Each source's name or id for a team, mapped to its slug."""

    by_cbbd_id: Mapping[int, str]
    by_torvik_name: Mapping[str, str]
    by_espn_id: Mapping[str, str]

    @classmethod
    def from_teams(cls, teams: Sequence[CanonicalTeam]) -> Self:
        return cls(
            by_cbbd_id={team.cbbd_id: team.slug for team in teams},
            by_torvik_name={team.torvik_name: team.slug for team in teams},
            by_espn_id={team.espn_id: team.slug for team in teams},
        )

    def slug_for_cbbd(self, cbbd_id: int) -> str | None:
        return self.by_cbbd_id.get(cbbd_id)

    def slug_for_torvik(self, name: str) -> str | None:
        return self.by_torvik_name.get(name)

    def slug_for_espn(self, espn_id: str) -> str | None:
        return self.by_espn_id.get(espn_id)
