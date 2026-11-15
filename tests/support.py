"""Helpers shared by the test suite: building tiny GeoNames-shaped dumps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PLACE_FIELDS: tuple[str, ...] = (
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "featureclass",
    "featurecode",
    "countrycode",
    "cc2",
    "admin1code",
    "admin2code",
    "admin3code",
    "admin4code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modificationdate",
)


def dump_row(
    geonameid: int,
    asciiname: str,
    latitude: float,
    longitude: float,
    *,
    name: str | None = None,
    alternatenames: str = "",
    countrycode: str = "XX",
    population: int = 0,
    featurecode: str = "PPL",
) -> dict[str, Any]:
    """One place, as the tool sees it. Only the fields we use are meaningful."""
    return {
        "geonameid": geonameid,
        "name": name if name is not None else asciiname,
        "asciiname": asciiname,
        "alternatenames": alternatenames,
        "latitude": latitude,
        "longitude": longitude,
        "featureclass": "P",
        "featurecode": featurecode,
        "countrycode": countrycode,
        "cc2": "",
        "admin1code": "",
        "admin2code": "",
        "admin3code": "",
        "admin4code": "",
        "population": population,
        "elevation": "",
        "dem": "",
        "timezone": "UTC",
        "modificationdate": "2024-01-01",
    }


def dump_text(rows: list[dict[str, Any]]) -> str:
    return "".join("\t".join(str(row[field]) for field in PLACE_FIELDS) + "\n" for row in rows)


def write_dump(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a GeoNames-shaped dump, tab-separated and UTF-8, as the real ones are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_text(rows), encoding="utf-8", newline="\n")
    return path


def import_dump(
    database: Path, dump: Path, *, source_threshold: int | None = None, recreate: bool = False
):
    """Run the real import over one dump and hand back an open connection to the result.

    The tests go through the production import rather than poking rows in with SQL, so a
    table the tests built by hand cannot drift from a table the tool builds.
    """
    from distnsimilarity.db import connect, import_places
    from distnsimilarity.datasets import dataset_threshold

    connection = connect(database)
    threshold = (
        dataset_threshold(dump.name) if source_threshold is None else source_threshold
    )
    import_places(
        connection,
        [(threshold, dump)],
        recreate_database=recreate,
    )
    return connection


def dump_fields(**overrides: object) -> list[str]:
    """One dump line as raw tab-separated fields, so a test can break one of them.

    `write_dump` rebuilds a row from keyword arguments and cannot express a line that is
    missing a column, or a column holding words where a number belongs; this can.
    """
    unknown = set(overrides) - set(PLACE_FIELDS)
    if unknown:
        raise KeyError(f"not dump fields: {sorted(unknown)}")
    row = dump_row(1, "Name", 0.0, 0.0)
    row.update({key: value for key, value in overrides.items()})
    return [str(row[field]) for field in PLACE_FIELDS]


def write_dump_lines(path: Path, lines: list[list[str]]) -> Path:
    """Write raw field lists as a dump, one row per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join("\t".join(line) for line in lines) + "\n", encoding="utf-8")
    return path
