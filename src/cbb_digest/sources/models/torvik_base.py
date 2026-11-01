"""Positional rows: Torvik's player files carry no header, so each model maps names to indices.

The column order is undocumented and can change mid-season (spec §6). A row with the wrong number
of fields is a `ShapeError`, never a best-effort parse of whatever the indices now hold.
"""

from collections.abc import Mapping


class ShapeError(ValueError):
    """A Torvik row or body whose layout no longer matches its column map.

    `fetch_source` turns it into `SourceFailure("torvik", "invalid response: shape: …")`.
    """


def pick(row: object, columns: Mapping[str, int], expected: int, number: int) -> dict[str, object]:
    """The named fields of row `number` (1-based), once it is a list of `expected` fields."""
    if not isinstance(row, list):
        raise ShapeError(f"shape: row {number} is {type(row).__name__}, expected a list")
    fields: list[object] = row  # pyright: ignore[reportUnknownVariableType]
    if len(fields) != expected:
        raise ShapeError(f"shape: row {number} has {len(fields)} fields, expected {expected}")
    return {name: fields[index] for name, index in columns.items()}
