"""Candidate generation on the database, and scanning it.

The equal-length prefilter stops being a comparison here and becomes the shape of the
index: places are fetched a name-length at a time, already restricted to the population
floor, so a pair of unequal-length names is never even read out of the table.

The population floor is applied to *candidates*, never to results - it decides what gets
compared, which is why a higher floor makes a search shorter rather than merely shorter to
display.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from distnsimilarity.core.places import Place
from distnsimilarity.core.prefilter import all_pair_count
from distnsimilarity.core.scan import MatchedPair, matching_pairs
from distnsimilarity.db.schema import (
    PLACE_SELECT,
    DatabaseNotImportedError,
    place_count,
    row_to_place,
)

#: Places fetched per query. A name-length group has to be held while it is walked, because
#: each place is compared against the places after it, but it is fetched in pages so no one
#: query pulls the whole table at once. The page has to be big enough to make a query worth
#: its while: 2,000 places is a few tens of kilobytes and keeps a bucket to a handful of
#: queries.
DEFAULT_PAGE_SIZE = 2_000


@dataclass(frozen=True)
class NameLengthGroup:
    """How many candidates have an ASCII name of this length."""

    name_length: int
    places: int

    @property
    def pairs(self) -> int:
        """Pairs this group contributes: each place against itself and the later places."""
        return all_pair_count(self.places)


@dataclass(frozen=True)
class SearchScope:
    """What a search is about to compare, so an interface can say it out before running."""

    places: int
    population_floor: int
    groups: tuple[NameLengthGroup, ...]

    @property
    def pair_count(self) -> int:
        """Pairs the length groups hold - what the scan will actually compare."""
        return sum(group.pairs for group in self.groups)

    @property
    def full_pair_count(self) -> int:
        """Pairs a comparison without the length groups would have considered."""
        return all_pair_count(self.places)

    @property
    def skipped_share(self) -> float:
        """How much of that work the length groups never look at."""
        if self.full_pair_count == 0:
            return 0.0
        return 1.0 - (self.pair_count / self.full_pair_count)

    def summary(self) -> str:
        return (
            f"{self.places} place(s) at population >= {self.population_floor}, "
            f"{len(self.groups)} name length(s), {self.pair_count} pair(s) to compare "
            f"instead of {self.full_pair_count}"
        )


def effective_population_floor(
    connection: sqlite3.Connection, population_floor: int | None
) -> int:
    """The floor a search really applies: the caller's, or the table's own minimum."""
    if population_floor is not None:
        return population_floor
    row = connection.execute("SELECT MIN(population) FROM place").fetchone()
    return int(row[0]) if row[0] is not None else 0


def name_length_groups(
    connection: sqlite3.Connection, population_floor: int | None = None
) -> tuple[NameLengthGroup, ...]:
    """Candidate places grouped by ASCII-name length, shortest name first."""
    where, parameters = _floor_clause(population_floor)
    rows = connection.execute(
        f"SELECT name_length, COUNT(*) AS places FROM place {where} "
        "GROUP BY name_length ORDER BY name_length",
        parameters,
    ).fetchall()
    return tuple(NameLengthGroup(int(row["name_length"]), int(row["places"])) for row in rows)


def scope_for(
    connection: sqlite3.Connection, population_floor: int | None = None
) -> SearchScope:
    """The place count, effective floor and length groups a search will use."""
    groups = name_length_groups(connection, population_floor)
    return SearchScope(
        places=sum(group.places for group in groups),
        population_floor=effective_population_floor(connection, population_floor),
        groups=groups,
    )


def candidate_name_lengths(
    connection: sqlite3.Connection, population_floor: int | None = None
) -> list[int]:
    """Every name length that has at least one candidate at this floor."""
    return [group.name_length for group in name_length_groups(connection, population_floor)]


def _floor_clause(population_floor: int | None) -> tuple[str, tuple[object, ...]]:
    if population_floor is None:
        return "", ()
    return "WHERE population >= ?", (population_floor,)


def candidate_places(
    connection: sqlite3.Connection,
    population_floor: int | None = None,
    *,
    name_length: int | None = None,
    exclude_geonameid: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[Place]:
    """Candidates in a stable order - population ascending, GeoNames id to break ties.

    One name length at a time when `name_length` is given, which is the lookup the
    `(name_length, population, geonameid)` index exists for. The order is what lets the
    walk page through a bucket with a keyset cursor instead of an offset, so a big bucket
    stays a stream and a repeated search returns the same places in the same order.
    """
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, got {page_size}")

    clauses: list[str] = []
    parameters: list[object] = []
    if name_length is not None:
        clauses.append("name_length = ?")
        parameters.append(name_length)
    if population_floor is not None:
        clauses.append("population >= ?")
        parameters.append(population_floor)
    if exclude_geonameid is not None:
        clauses.append("geonameid <> ?")
        parameters.append(exclude_geonameid)

    cursor: tuple[int, int] | None = None
    while True:
        conditions = list(clauses)
        page_parameters = list(parameters)
        if cursor is not None:
            conditions.append("(population > ? OR (population = ? AND geonameid > ?))")
            page_parameters.extend([cursor[0], cursor[0], cursor[1]])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = connection.execute(
            f"{PLACE_SELECT} {where} ORDER BY population, geonameid LIMIT ?",
            [*page_parameters, page_size],
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row_to_place(row)
        if len(rows) < page_size:
            return
        cursor = (int(rows[-1]["population"]), int(rows[-1]["geonameid"]))


def places_with_ascii_name(connection: sqlite3.Connection, asciiname: str) -> list[Place]:
    """Every place whose ASCII name is exactly this, GeoNames id order.

    Searching from a place starts here: two places can share an ASCII name, so a name is a
    question, not an answer, and the caller has to be shown the choices.
    """
    rows = connection.execute(
        f"{PLACE_SELECT} WHERE asciiname = ? ORDER BY geonameid",
        (asciiname,),
    ).fetchall()
    return [row_to_place(row) for row in rows]


def scan_database(
    connection: sqlite3.Connection,
    character_difference: int,
    population_floor: int | None = None,
    *,
    exclude_geonameid: int | None = None,
    equal_length_prefilter: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[MatchedPair]:
    """Every matching pair in the database, one name-length group at a time.

    Pairs come out group by group - all the 4-character names, then all the 5-character
    ones - and within a group by ascending population, a place before the places it is
    compared against. A place is a pair with itself, as it has always been, so a threshold
    of 0 reports each candidate once at a distance of nothing.

    With `equal_length_prefilter` off, every candidate is compared against every other one
    as it was before the prefilter existed; the pairs reported are the same either way,
    which is what `tests/test_database_candidates.py` checks in both directions.
    """
    if place_count(connection) == 0:
        raise DatabaseNotImportedError(
            "the place table is empty: run distnsimilarity-import before searching"
        )

    if not equal_length_prefilter:
        everything = list(
            candidate_places(
                connection,
                population_floor,
                exclude_geonameid=exclude_geonameid,
                page_size=page_size,
            )
        )
        yield from _scan_group(everything, character_difference)
        return

    for name_length in candidate_name_lengths(connection, population_floor):
        group = list(
            candidate_places(
                connection,
                population_floor,
                name_length=name_length,
                exclude_geonameid=exclude_geonameid,
                page_size=page_size,
            )
        )
        yield from _scan_group(group, character_difference)


def _scan_group(group: list[Place], character_difference: int) -> Iterator[MatchedPair]:
    """One group, walked the way a scan walks any candidate list."""
    for index, first in enumerate(group):
        yield from matching_pairs(first, group[index:], character_difference)
