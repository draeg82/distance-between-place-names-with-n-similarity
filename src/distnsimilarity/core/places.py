"""Places: the GeoNames dump row, and reading one from a tab-separated dump."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DUMP_FIELD_COUNT = 19


class PlaceRowError(ValueError):
    """A dump row that cannot be turned into a place, naming the line at fault."""


@dataclass(frozen=True)
class Place:
    """One place: a GeoNames record with coordinates, not a name.

    ``asciiname`` is the sole input to matching; ``display_name`` and the alternate names
    are carried for display and are never compared.
    """

    geonameid: int
    asciiname: str
    display_name: str
    latitude: float
    longitude: float
    population: int
    country_code: str

    @property
    def name_length(self) -> int:
        return len(self.asciiname)


@dataclass(frozen=True)
class UnreadableRow:
    """A row the tool refused, and why."""

    line_number: int
    reason: str
    content: str


def _column(
    fields: list[str],
    index: int,
    column: str,
    line_number: int,
    converter: type[int] | type[float],
    expected: str,
) -> int | float:
    """One numeric column, with the column named in whatever refusal it produces."""
    raw = fields[index]
    try:
        return converter(raw)
    except ValueError as error:
        raise PlaceRowError(
            f"line {line_number}: column {index + 1} ({column}) is not {expected}: {raw!r}"
        ) from error


def _parse_row(fields: list[str], line_number: int) -> Place:
    if len(fields) != DUMP_FIELD_COUNT:
        raise PlaceRowError(
            f"line {line_number}: expected {DUMP_FIELD_COUNT} tab-separated fields, got {len(fields)}"
        )
    _geonameid_field, name, asciiname, _alternates = fields[:4]
    population_field = fields[14]
    place = Place(
        geonameid=int(_column(fields, 0, "geonameid", line_number, int, "an integer")),
        display_name=name,
        asciiname=asciiname,
        latitude=float(_column(fields, 4, "latitude", line_number, float, "a number")),
        longitude=float(_column(fields, 5, "longitude", line_number, float, "a number")),
        population=(
            0
            if not population_field.strip()
            else int(_column(fields, 14, "population", line_number, int, "an integer"))
        ),
        country_code=fields[8],
    )
    if not -90.0 <= place.latitude <= 90.0:
        raise PlaceRowError(f"line {line_number}: latitude {place.latitude} out of range")
    if not -180.0 <= place.longitude <= 180.0:
        raise PlaceRowError(f"line {line_number}: longitude {place.longitude} out of range")
    return place


def _iter_rows(path: Path) -> Iterator[tuple[int, list[str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for line_number, fields in enumerate(reader, start=1):
            if not fields:
                continue
            yield line_number, fields


def read_places(path: Path) -> list[Place]:
    """Every place in a dump, in file order. Raises ``PlaceRowError`` on the first bad row."""
    return [_parse_row(fields, line_number) for line_number, fields in _iter_rows(path)]


def read_places_lenient(path: Path) -> tuple[list[Place], list[UnreadableRow]]:
    """Every readable place, plus the rows that were refused instead of silently dropped."""
    places: list[Place] = []
    problems: list[UnreadableRow] = []
    for line_number, fields in _iter_rows(path):
        try:
            places.append(_parse_row(fields, line_number))
        except PlaceRowError as error:
            problems.append(
                UnreadableRow(
                    line_number,
                    str(error).split(": ", 1)[-1],
                    "\t".join(fields),
                )
            )
    return places, problems
