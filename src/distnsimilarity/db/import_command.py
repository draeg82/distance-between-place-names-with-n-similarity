"""The import command: build the place database from GeoNames dumps.

    distnsimilarity-import                       # every dump in the data directory
    distnsimilarity-import --dataset 5000        # one dump
    distnsimilarity-import --recreate            # throw the table away and rebuild it

The database is generated, never committed. It lives beside the dumps unless told
otherwise, which means the default location is found the same portable way the CLI finds a
dataset - never a home directory.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from distnsimilarity.db.importing import import_places
from distnsimilarity.db.schema import DatabaseSchemaError, connect, default_database_path
from distnsimilarity.datasets import (
    DatasetNotAvailableError,
    available_datasets,
    dataset_threshold,
    resolve_dataset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distnsimilarity-import",
        description=(
            "Build the place database from GeoNames dumps, deduplicated by GeoNames id. "
            "Every dump contains the ones above it, so importing all of them adds each "
            "place once."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help=(
            "Where the SQLite database goes. Default: places.sqlite3 beside the dumps. "
            "No home directory is ever guessed."
        ),
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=None,
        metavar="REQUEST",
        help=(
            "Dump to import: population threshold (15000), file name (cities15000.txt) or a "
            "path. Repeatable. Default: every dump in the data directory."
        ),
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop the place table and rebuild it from the dumps given.",
    )
    return parser


def dumps_requested(requests: Sequence[str]) -> list[tuple[int, Path]]:
    """The dumps a caller named, as `(population threshold, path)`.

    The same dump asked for twice, or twice under different names - `5000` and
    `cities5000.txt` - is one dump, recorded at the smallest threshold it was named by.
    """
    by_path: dict[Path, int] = {}
    for request in requests:
        path = resolve_dataset(request)
        threshold = dataset_threshold(path.name)
        if path not in by_path or threshold < by_path[path]:
            by_path[path] = threshold
    return [(threshold, path) for path, threshold in by_path.items()]


def every_dump() -> list[tuple[int, Path]]:
    """Every dump in the data directory, or a complaint naming the flag that fixes it."""
    available = available_datasets()
    if not available:
        raise DatasetNotAvailableError(
            "no dumps to import: none of cities*.txt are in the data directory; pass "
            "--dataset PATH (or set DISTNSIMILARITY_DATA_DIR)"
        )
    return [(int(dataset.threshold), dataset.path) for dataset in available]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    try:
        dumps = dumps_requested(arguments.dataset) if arguments.dataset else every_dump()
        database = (
            default_database_path()
            if arguments.database is None
            else Path(arguments.database).expanduser()
        )
    except DatasetNotAvailableError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    try:
        connection = connect(database)
    except DatabaseSchemaError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    try:
        report = import_places(connection, dumps, recreate_database=arguments.recreate)
    except sqlite3.Error as error:
        print(
            f"error: could not write {database}: {error}; if it holds a table this version "
            "does not recognise, try --recreate",
            file=sys.stderr,
        )
        return 1
    finally:
        connection.close()

    print(report.summary())
    for line in report.problem_lines():
        print(f"error: refused {line}", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
