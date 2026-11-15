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


def _parse_row(fields: list[str], line_number: int) -> Place:
    if len(fields) != DUMP_FIELD_COUNT:
        raise PlaceRowError(
            f"line {line_number}: expected {DUMP_FIELD_COUNT} tab-separated fields, got {len(fields)}"
        )
    geonameid, name, asciiname, _alternates, latitude, longitude = fields[:6]
    country_code = fields[8]
    population = fields[14]
    try:
        place = Place(
            geonameid=int(geonameid),
            display_name=name,
            asciiname=asciiname,
            latitude=float(latitude),
            longitude=float(longitude),
            population=int(population) if population.strip() else 0,
            country_code=country_code,
        )
    except ValueError as error:
        raise PlaceRowError(f"line {line_number}: {error} in {fields!r}") from error
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
            problems.append(UnreadableRow(line_number, str(error).split(": ", 1)[-1], "\t".join(fields)))
    return places, problems
