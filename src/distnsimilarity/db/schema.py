"""The place table and its indexes.

The database is generated from the GeoNames dumps by ``distnsimilarity-import`` and is
never committed. One row per GeoNames id: the three committed dumps contain each other, so
230,160 input rows become 146,782 places here rather than 230,160.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from distnsimilarity.core.places import Place
from distnsimilarity.datasets import default_data_dir

DEFAULT_DATABASE_NAME = "places.sqlite3"

#: Bump when the shape below changes; a database from a newer version is refused rather
#: than misread.
SCHEMA_VERSION = 1

PLACE_COLUMNS: tuple[str, ...] = (
    "geonameid",
    "asciiname",
    "display_name",
    "latitude",
    "longitude",
    "population",
    "country_code",
    "name_length",
    "source_threshold",
)

#: The columns a core `Place` needs: the table minus the two the database adds for itself
#: (`name_length`, which the index is made of, and `source_threshold`, which is provenance).
PLACE_READ_COLUMNS: tuple[str, ...] = tuple(
    column for column in PLACE_COLUMNS if column not in ("name_length", "source_threshold")
)

#: The one way a `Place` is asked for out of the table, so the column list is written once.
PLACE_SELECT = f"SELECT {', '.join(PLACE_READ_COLUMNS)} FROM place"

PLACE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS place (
    geonameid        INTEGER NOT NULL PRIMARY KEY,
    asciiname        TEXT    NOT NULL,
    display_name     TEXT    NOT NULL,
    latitude         REAL    NOT NULL,
    longitude        REAL    NOT NULL,
    population       INTEGER NOT NULL,
    country_code     TEXT    NOT NULL,
    name_length      INTEGER NOT NULL,
    source_threshold INTEGER NOT NULL
)
"""

#: Both search modes ask the same shape of question - "places whose ASCII name is this
#: long and whose population is at least this" - so the composite index leads with the
#: length. The population index serves the counts the interface shows before a search, and
#: the name index serves finding a place by name.
INDEX_DDL: tuple[tuple[str, str], ...] = (
    (
        "idx_place_name_length_population",
        "CREATE INDEX IF NOT EXISTS idx_place_name_length_population "
        "ON place (name_length, population, geonameid)",
    ),
    (
        "idx_place_population",
        "CREATE INDEX IF NOT EXISTS idx_place_population ON place (population)",
    ),
    (
        "idx_place_asciiname",
        "CREATE INDEX IF NOT EXISTS idx_place_asciiname ON place (asciiname)",
    ),
)

INDEX_NAMES: tuple[str, ...] = tuple(name for name, _ in INDEX_DDL)


class DatabaseSchemaError(RuntimeError):
    """The database on disk is not a place database this version can read."""


class DatabaseNotImportedError(RuntimeError):
    """The database is empty: there is nothing to search yet."""


def default_database_path(data_dir: Path | None = None) -> Path:
    """``places.sqlite3`` beside the dumps, unless told otherwise.

    The dumps directory is found the same way the CLI finds a dataset, so this never
    resolves against a home directory.
    """
    return (default_data_dir() if data_dir is None else data_dir) / DEFAULT_DATABASE_NAME


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database, with rows by name, creating the table if it is not there.

    An empty database is a real state rather than a broken one: it is the state a machine in
    `fresh clone, before the import` is in. Creating the schema here means a search reports
    `nothing has been imported yet` instead of failing on a missing table, and a database
    written by a newer version is refused here, at the door.
    """
    database = Path(path).expanduser()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database))
    # A read-mostly database that the search API can read while a scan is running.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the table and indexes if absent, and check the version if present."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise DatabaseSchemaError(
            f"this database was written by a newer version (schema version {version}, this "
            f"is {SCHEMA_VERSION}); re-import it instead of reading it here"
        )
    connection.execute(PLACE_TABLE_DDL)
    for _, statement in INDEX_DDL:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def recreate(connection: sqlite3.Connection) -> None:
    """Drop the place table and build it again. Its indexes go with the table."""
    connection.execute("DROP TABLE IF EXISTS place")
    connection.commit()
    ensure_schema(connection)


def place_values(place: Place, source_threshold: int) -> tuple[object, ...]:
    """One row, in `PLACE_COLUMNS` order."""
    return (
        place.geonameid,
        place.asciiname,
        place.display_name,
        place.latitude,
        place.longitude,
        place.population,
        place.country_code,
        place.name_length,
        source_threshold,
    )


def row_to_place(row: sqlite3.Row) -> Place:
    """A row from the place table, as a core `Place`."""
    return Place(
        geonameid=int(row["geonameid"]),
        asciiname=str(row["asciiname"]),
        display_name=str(row["display_name"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        population=int(row["population"]),
        country_code=str(row["country_code"]),
    )


def place_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM place").fetchone()[0])


def index_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return sorted(str(row["name"]) for row in rows)


def query_plan(
    connection: sqlite3.Connection, statement: str, parameters: Sequence[object] = ()
) -> str:
    """How SQLite says it will run a statement - a test's way of noticing a table scan."""
    rows = connection.execute(f"EXPLAIN QUERY PLAN {statement}", tuple(parameters)).fetchall()
    return "\n".join(str(row["detail"]) for row in rows)
