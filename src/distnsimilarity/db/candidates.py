"""Candidate generation on the database, and scanning it.

The equal-length prefilter stops being a comparison here and becomes the shape of the
index: places are fetched a name-length at a time, already restricted to the population
floor, so a pair of unequal-length names is never even read out of the table.

The population floor is applied to *candidates*, never to results - it decides what gets
compared, which is why a higher floor makes a search shorter rather than merely shorter to
display.

A *dataset threshold* is the other way a search says what it covers, and it is applied the
same way: `population >= threshold`, the GeoNames rule behind `cities5000.txt`. It is a
floor chosen from a list rather than one typed in, which is what makes the two controls
different in kind and not in effect - see `dataset_options` for the list, and ADR 0007.

Reading one place out, or reading every place that holds a typed name, is not candidate
generation and lives in `db.place_lookup`: a name is a question about spelling and a `geonameid`
is an answer, and only the second one can be searched from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from distnsimilarity.core.places import Place
from distnsimilarity.core.prefilter import all_pair_count
from distnsimilarity.datasets import WHOLE_DATABASE_LABEL, dataset_label
from distnsimilarity.core.scan import (
    POLL_EVERY_COMPARISONS,
    CancelCheck,
    MatchedPair,
    Progress,
    matching_pairs,
)
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
    dataset_threshold: int | None = None

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
        scope = f"population >= {self.population_floor}"
        if self.dataset_threshold is not None:
            scope = f"{scope}, dataset threshold {self.dataset_threshold}"
        return (
            f"{self.places} place(s) at {scope}, "
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
    connection: sqlite3.Connection,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
) -> tuple[NameLengthGroup, ...]:
    """Candidate places grouped by ASCII-name length, shortest name first."""
    where, parameters = _candidate_clause(population_floor, dataset_threshold)
    rows = connection.execute(
        f"SELECT name_length, COUNT(*) AS places FROM place {where} "
        "GROUP BY name_length ORDER BY name_length",
        parameters,
    ).fetchall()
    return tuple(NameLengthGroup(int(row["name_length"]), int(row["places"])) for row in rows)


def scope_for(
    connection: sqlite3.Connection,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
) -> SearchScope:
    """The place count, effective floor and length groups a search will use.

    The floor reported is the one the counts were taken at - the stricter of the caller's
    floor and the dataset - so a scope cannot say one floor and have compared at another.
    """
    groups = name_length_groups(
        connection, population_floor, dataset_threshold=dataset_threshold
    )
    return SearchScope(
        places=sum(group.places for group in groups),
        population_floor=effective_population_floor(
            connection, candidate_floor(population_floor, dataset_threshold)
        ),
        groups=groups,
        dataset_threshold=dataset_threshold,
    )


def candidate_name_lengths(
    connection: sqlite3.Connection,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
) -> list[int]:
    """Every name length that has at least one candidate at this floor."""
    return [
        group.name_length
        for group in name_length_groups(
            connection, population_floor, dataset_threshold=dataset_threshold
        )
    ]


def candidate_floor(
    population_floor: int | None, dataset_threshold: int | None = None
) -> int | None:
    """The one population floor both filters collapse to.

    A dataset threshold and a population floor are floors on the same column, so a request
    giving both means the stricter of the two: `dataset=5000&min_population=20000` compares
    the cities of 20,000 or more people, and cannot silently widen to the 5,000-rule. Keeping
    the pair in the request is what lets a dataset dropdown and a typed floor coexist without
    either being ignored.
    """
    floors = [floor for floor in (population_floor, dataset_threshold) if floor is not None]
    return max(floors) if floors else None


def _candidate_clause(
    population_floor: int | None, dataset_threshold: int | None = None
) -> tuple[str, tuple[object, ...]]:
    """The one WHERE builder a counting query is made of."""
    floor = candidate_floor(population_floor, dataset_threshold)
    if floor is None:
        return "", ()
    return "WHERE population >= ?", (floor,)


def candidate_places(
    connection: sqlite3.Connection,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
    name_length: int | None = None,
    exclude_geonameid: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[Place]:
    """Candidates in a stable order - population ascending, GeoNames id to break ties.

    One name length at a time when `name_length` is given, which is the lookup the
    `(name_length, population, geonameid)` index exists for. The order is what lets the
    walk page through a bucket with a keyset cursor instead of an offset, so a big bucket
    stays a stream and a repeated search returns the same places in the same order.

    A dataset threshold is folded into the population floor rather than added as a second
    clause, so the index seek is unchanged: both are `population >=` on the same column.
    """
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, got {page_size}")

    clauses: list[str] = []
    parameters: list[object] = []
    if name_length is not None:
        clauses.append("name_length = ?")
        parameters.append(name_length)
    floor = candidate_floor(population_floor, dataset_threshold)
    if floor is not None:
        clauses.append("population >= ?")
        parameters.append(floor)
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


def scan_database(
    connection: sqlite3.Connection,
    character_difference: int,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
    exclude_geonameid: int | None = None,
    equal_length_prefilter: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
    poll_every_comparisons: int = POLL_EVERY_COMPARISONS,
) -> Iterator[MatchedPair]:
    """Every matching pair in the database, one name-length group at a time.

    Pairs come out group by group - all the 4-character names, then all the 5-character
    ones - and within a group by ascending population, a place before the places it is
    compared against. A place is a pair with itself, as it has always been, so a threshold
    of 0 reports each candidate once at a distance of nothing.

    `population_floor` and `dataset_threshold` are the two floors a request can carry, and
    both are applied here rather than to the pairs: see `candidate_floor`.

    With `equal_length_prefilter` off, every candidate is compared against every other one
    as it was before the prefilter existed; the pairs reported are the same either way,
    which is what `tests/test_database_candidates.py` checks in both directions.

    `cancel` stops the walk between two comparisons and between two candidate places, the
    same check the file-backed scan makes: a cancelled search stops fetching name-length groups
    as well as comparing them, and does not walk the rest of a group to find out.
    `progress` counts the comparisons it made, once per poll window.
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
                dataset_threshold=dataset_threshold,
                exclude_geonameid=exclude_geonameid,
                page_size=page_size,
            )
        )
        yield from _scan_group(
            everything,
            character_difference,
            cancel=cancel,
            progress=progress,
            poll_every_comparisons=poll_every_comparisons,
        )
        return

    for name_length in candidate_name_lengths(
        connection, population_floor, dataset_threshold=dataset_threshold
    ):
        if cancel is not None and cancel():
            return
        group = list(
            candidate_places(
                connection,
                population_floor,
                dataset_threshold=dataset_threshold,
                name_length=name_length,
                exclude_geonameid=exclude_geonameid,
                page_size=page_size,
            )
        )
        yield from _scan_group(
            group,
            character_difference,
            cancel=cancel,
            progress=progress,
            poll_every_comparisons=poll_every_comparisons,
        )


def _scan_group(
    group: list[Place],
    character_difference: int,
    *,
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
    poll_every_comparisons: int = POLL_EVERY_COMPARISONS,
) -> Iterator[MatchedPair]:
    """One group, walked the way a scan walks any candidate list.

    The cancel check between places is what a name-length group needs and a single scan loop
    does not give it: a group can hold 40,000 places, and walking to the end of one to discover
    that the search was cancelled two places back is work the answer has already refused.
    """
    for index, first in enumerate(group):
        if cancel is not None and cancel():
            return
        yield from matching_pairs(
            first,
            group[index:],
            character_difference,
            cancel=cancel,
            progress=progress,
            poll_every_comparisons=poll_every_comparisons,
        )


# The dataset a search runs over.
#
# A dataset is a population rule in words - "cities of 5,000 or more people" - and the rule is
# GeoNames' own: `cities5000.txt` is that list, so searching the cache at threshold 5000 is the
# same set that dump used to be, deduplicated. `source_threshold` is *not* that: it records the
# most inclusive dump each place arrived in, which is a fact about an import run rather than
# about a place, so it is listed as provenance and never used as the filter.


#: The population rules a person can choose. These are the GeoNames dumps this repository
#: knows how to import; a cache holds whatever it was imported from, and an option is only
#: offered when the places in the table make it a different set from the whole cache.
DATASET_THRESHOLDS: tuple[int, ...] = (1_000, 5_000, 15_000)


def places_at_least(connection: sqlite3.Connection, floor: int | None) -> int:
    """Places the table holds at this population floor - the size of a dataset choice."""
    where, parameters = _candidate_clause(floor)
    row = connection.execute(f"SELECT COUNT(*) FROM place {where}", parameters).fetchone()
    return int(row[0])


def candidate_pairs_at_least(connection: sqlite3.Connection, floor: int | None) -> int:
    """What a search at this floor would compare, counted from the name-length groups.

    It is the same count `scope_for` reports for the same floor, which is what lets a dropdown
    say "this choice is 3,317,056 comparisons" before a search is started rather than after.
    """
    groups = name_length_groups(connection, floor)
    return sum(group.pairs for group in groups)


def partners_at_name_length(
    connection: sqlite3.Connection,
    name_length: int,
    population_floor: int | None = None,
    *,
    dataset_threshold: int | None = None,
    excluding: int | None = None,
) -> int:
    """How many places a selected place has to be compared with: its partner set.

    A search from one place compares it against the candidates holding its own name length - the
    only names it can have a character difference with - which is one name-length group instead of
    every pair in the candidate set. That is why the cost of a from-a-place search is said in
    single comparisons rather than in millions of pairs, and why it can be answered off the
    `(name_length, population)` index instead of by counting.

    `excluding` is the selected place's own id: it is in its own name-length group, and a place is
    not a partner of itself, so counting it would quote a cost that includes a comparison the
    search will never make.
    """
    clauses = ["name_length = ?"]
    parameters: list[object] = [name_length]
    floor = candidate_floor(population_floor, dataset_threshold)
    if floor is not None:
        clauses.append("population >= ?")
        parameters.append(floor)
    if excluding is not None:
        clauses.append("geonameid <> ?")
        parameters.append(excluding)
    row = connection.execute(
        f"SELECT COUNT(*) FROM place WHERE {' AND '.join(clauses)}", parameters
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True)
class DatasetOption:
    """One entry in the dataset choice: the rule, its words, and what searching it costs.

    `threshold` is None for the whole cache, which is the option a person picks when they want
    every place including the ones GeoNames lists below 1,000 - the bare number is a secondary
    field, never the label.
    """

    threshold: int | None
    label: str
    places: int
    candidate_pairs: int

    def as_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "label": self.label,
            "places": self.places,
            "candidate_pairs": self.candidate_pairs,
        }


def whole_cache_option(connection: sqlite3.Connection) -> DatasetOption:
    """The dataset choice for a search over every place in the cache.

    The count is a field of its own, so the label stays the one phrase every other place in the
    tool uses for this choice instead of repeating a number that will have moved by the time a
    person reads it.
    """
    places = place_count(connection)
    return DatasetOption(
        threshold=None,
        label=WHOLE_DATABASE_LABEL,
        places=places,
        candidate_pairs=candidate_pairs_at_least(connection, None),
    )


def dataset_options(connection: sqlite3.Connection) -> tuple[DatasetOption, ...]:
    """Every dataset this cache can offer: the whole cache first, then its own thresholds.

    The choices are built from the thresholds this tool names (1,000, 5,000, 15,000) rather than
    from the `source_threshold` each row was imported under, because a dataset is a rule about
    how big a city has to be and not a record of which file it arrived in: a cache that holds
    cities1000 and cities15000 still offers cities of 5,000 or more people, since the people are
    in there.

    A choice is left out when it would search exactly the places a choice already on the list
    searches - which is what a threshold every place in the cache clears amounts to, as 1,000 is
    for a cache imported from cities1000 alone. Two thresholds hold the same places exactly when
    they hold the same number of them, because a bigger threshold only ever takes places away, so
    counting is enough to know that two choices would mean the same search.
    """
    whole = whole_cache_option(connection)
    options: list[DatasetOption] = [whole]
    for threshold in DATASET_THRESHOLDS:
        places = places_at_least(connection, threshold)
        if any(option.places == places for option in options):
            continue
        options.append(
            DatasetOption(
                threshold=threshold,
                label=dataset_label(threshold),
                places=places,
                candidate_pairs=candidate_pairs_at_least(connection, threshold),
            )
        )
    return tuple(options)
