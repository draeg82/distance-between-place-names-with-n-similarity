"""The generated place database: one table of deduplicated places, and the candidate
lookups both search modes run on it."""

from distnsimilarity.db.candidates import (
    DEFAULT_PAGE_SIZE,
    NameLengthGroup,
    SearchScope,
    candidate_name_lengths,
    candidate_places,
    effective_population_floor,
    name_length_groups,
    places_with_ascii_name,
    scan_database,
    scope_for,
)
from distnsimilarity.db.importing import ImportReport, dumps_to_import, import_places
from distnsimilarity.db.schema import (
    DEFAULT_DATABASE_NAME,
    INDEX_NAMES,
    SCHEMA_VERSION,
    DatabaseNotImportedError,
    DatabaseSchemaError,
    connect,
    default_database_path,
    ensure_schema,
    place_count,
)

__all__ = [
    "DEFAULT_DATABASE_NAME",
    "DEFAULT_PAGE_SIZE",
    "DatabaseNotImportedError",
    "DatabaseSchemaError",
    "INDEX_NAMES",
    "ImportReport",
    "NameLengthGroup",
    "SCHEMA_VERSION",
    "SearchScope",
    "candidate_name_lengths",
    "candidate_places",
    "connect",
    "default_database_path",
    "dumps_to_import",
    "effective_population_floor",
    "ensure_schema",
    "import_places",
    "name_length_groups",
    "place_count",
    "places_with_ascii_name",
    "scan_database",
    "scope_for",
]
