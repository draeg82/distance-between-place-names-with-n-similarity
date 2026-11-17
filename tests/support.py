"""Helpers shared by the test suite: building tiny GeoNames-shaped dumps."""

from __future__ import annotations

import random
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


def place_database(
    path: Path, rows: list[dict[str, Any]], *, source_threshold: int = 1_000
) -> Path:
    """A place database holding exactly these places, imported from a throwaway dump.

    The database is the only search source, so every test that searches needs one, and it goes
    through the production import rather than a second way of filling a table the tool would
    never build. `source_threshold` says which dump that was: provenance, and what a test about
    the datasets a cache offers has to set.
    """
    dump = write_dump(path.parent / f"cities{source_threshold}.txt", rows)
    connection = import_dump(path, dump, source_threshold=source_threshold)
    connection.close()
    return path


def noisy_rows(
    *, places: int = 1_500, name_length: int = 7, seed: int = 20261116
) -> list[dict[str, Any]]:
    """The places `noisy_dump` writes, so a dump and a database can hold the same noise.

    Every name is the same length, so the equal-length prefilter cannot skip the noise and the
    scan has to walk it; the first two rows are a planted pair at `n=1`. The names come from a
    seeded generator, so the set is the same every time and a test can compare a stopped search
    against a full one.

    The planted pair sits at the lowest populations in the set on purpose: a dump is read in
    file order and a database in ascending population, and that is the only ordering both can
    agree on - so "the pair is at the head" is true of either, and `NOISY_DUMP_PAIR` names the
    places in the order a search reports them.
    """
    generator = random.Random(seed)
    rows = [
        dump_row(1, "abelard", 44.0, -93.0, population=1),
        dump_row(2, "abelurd", 39.0, -89.0, population=2),
    ]
    for index in range(3, places + 1):
        name = "".join(generator.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(name_length))
        rows.append(dump_row(index, name, index / 100.0, index / 50.0, population=1_000))
    return rows


def noisy_dump(
    path: Path, *, places: int = 1_500, name_length: int = 7, seed: int = 20261116
) -> Path:
    """A dump that starts with one pair and then searches for a while without finding one."""
    return write_dump(path, noisy_rows(places=places, name_length=name_length, seed=seed))


def noisy_database(
    path: Path, *, places: int = 1_500, name_length: int = 7, seed: int = 20261116
) -> Path:
    """`noisy_dump` imported: the same planted pair and the same noise, in a place database.

    Cancellation and streaming tests need a scan that takes noticeable time without being a real
    dataset, and the database is where a scan now happens.
    """
    return place_database(path, noisy_rows(places=places, name_length=name_length, seed=seed))


#: The pair `noisy_dump` plants, in the order a scan reports it (file order).
NOISY_DUMP_PAIR = ("abelard", "abelurd")

#: The character difference `noisy_dump` is planted to match at.
NOISY_CHARACTER_DIFFERENCE = 1
