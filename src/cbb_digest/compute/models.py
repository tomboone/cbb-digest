"""The digest's sections, field for field (spec §5).

Each section is its own model, so each builder can be tested on its own; the `Digest` that holds
them is assembled by the `compute` command.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

type MoverKind = Literal["moved", "entered", "left"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Mover(_Model):
    """One team in the movers section (spec §8.6).

    `rank` is `None` for a team that left the top 25 and is missing from this week's snapshot,
    and `prev_rank` is `None` for one that entered and was missing from last week's. `change` is
    `prev_rank - rank`, positive for a team that moved up, and `None` when either rank is.
    """

    slug: str
    display_name: str
    rank: int | None
    prev_rank: int | None
    change: int | None
    kind: MoverKind


class MoversSection(_Model):
    """Section 4: at most 10 movers, and how many more qualified.

    `baseline` is true when the season has no earlier snapshot to compare with, and then `rows`
    is empty and the email prints the baseline line instead.
    """

    rows: list[Mover] = []
    more_count: int = 0
    baseline: bool = False
