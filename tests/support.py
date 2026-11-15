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
