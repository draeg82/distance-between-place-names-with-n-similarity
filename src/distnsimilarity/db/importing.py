"""Building the place database from GeoNames dumps.

The three committed dumps contain each other, so importing all of them inserts each place
once: the smallest population threshold whose dump held a place is the one written into
`source_threshold`, and re-running the import changes nothing it already has.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from distnsimilarity.core.places import UnreadableRow, read_places_lenient
from distnsimilarity.db.schema import (
    PLACE_COLUMNS,
    ensure_schema,
    place_count,
    place_values,
    recreate,
)
from distnsimilarity.datasets import Dataset

#: One row per place, in `PLACE_COLUMNS` order. `INSERT OR IGNORE` makes the import
#: idempotent: a GeoNames id already in the table keeps the row it has.
INSERT_PLACE = (
    f"INSERT OR IGNORE INTO place ({', '.join(PLACE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(PLACE_COLUMNS))})"
)


def database_file(connection: sqlite3.Connection) -> str:
    """The file behind a connection, as SQLite itself says it.

    A report names the database it wrote rather than one a caller typed in: the same dump
    list imported twice under two spellings of a path should not produce two summaries that
    disagree about where the places went.
    """
    row = connection.execute("PRAGMA database_list").fetchone()
    return str(row[2]) if row[2] else ":memory:"


@dataclass(frozen=True)
class ImportReport:
    """What an import did, and every row it could not use."""

    database: str
    sources: tuple[str, ...]
    input_rows: int
    inserted: int
    unreadable: tuple[UnreadableRow, ...]
    places: int

    @property
    def deduplicated(self) -> int:
        """Readable rows that were not inserted, because their GeoNames id was already there."""
        return self.input_rows - len(self.unreadable) - self.inserted

    @property
    def exit_code(self) -> int:
        """Non-zero when rows were refused, so a scripted import cannot shrug them off."""
        return 1 if self.unreadable else 0

    def summary(self) -> str:
        sources = ", ".join(self.sources) if self.sources else "no dumps"
        return (
            f"{self.places} place(s) in {self.database} from {sources}: {self.input_rows} "
            f"row(s) read, {self.deduplicated} duplicate row(s) ignored, "
            f"{len(self.unreadable)} row(s) refused"
        )

    def problem_lines(self) -> list[str]:
        return [
            f"line {row.line_number} of {row.source}: {row.reason}" for row in self.unreadable
        ]


def _import_order(dumps: Sequence[tuple[int, Path]]) -> list[tuple[int, Path]]:
    """Smallest population threshold first, then file name: the first dump a place appears
    in is the one that records it, so the order of an import is not left to chance."""
    return sorted(dumps, key=lambda entry: (entry[0], entry[1].name))


def dumps_to_import(datasets: Sequence[Dataset]) -> list[tuple[int, Path]]:
    """`(population threshold, path)` per dump, smallest threshold first."""
    return _import_order([(int(dataset.threshold), dataset.path) for dataset in datasets])


def import_places(
    connection: sqlite3.Connection,
    dumps: Sequence[tuple[int, Path]],
    *,
    recreate_database: bool = False,
) -> ImportReport:
    """Import dumps into the place table, deduplicated by GeoNames id.

    Rows that cannot be read - unusable coordinates, missing fields, a non-numeric
    population - are reported per line and left out; they are never dropped in silence, and
    the report's exit code says so.
    """
    if recreate_database:
        recreate(connection)
    else:
        ensure_schema(connection)

    sources: list[str] = []
    input_rows = 0
    inserted = 0
    unreadable: list[UnreadableRow] = []
    try:
        for threshold, path in _import_order(dumps):
            places, problems = read_places_lenient(path)
            problems = [replace(problem, source=path.name) for problem in problems]
            unreadable.extend(problems)
            input_rows += len(places) + len(problems)
            sources.append(path.name)
            before = connection.total_changes
            connection.executemany(
                INSERT_PLACE, (place_values(place, threshold) for place in places)
            )
            inserted += connection.total_changes - before
        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return ImportReport(
        database=database_file(connection),
        sources=tuple(sources),
        input_rows=input_rows,
        inserted=inserted,
        unreadable=tuple(unreadable),
        places=place_count(connection),
    )
