"""Candidate generation on the database: the population floor picks what gets compared, the
name-length groups pick what never is, and both paths report the same pairs.

The floor is a *candidate* filter, not a display filter. That distinction is the whole
point of these tests: a floor that only trimmed the output would still pay for every
comparison and would hide pairs that the caller never knew were found.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pytest

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.places import Place, read_places
from distnsimilarity.core.scan import MatchedPair, scan
from distnsimilarity.db import (
    DatabaseNotImportedError,
    candidate_name_lengths,
    candidate_places,
    connect,
    import_places,
    places_with_ascii_name,
    scan_database,
    scope_for,
)
from distnsimilarity.datasets import dataset_threshold
from support import dump_row, import_dump, write_dump
from test_equal_length_prefilter import FIXTURE_CASES, NEAR_MISSES

REPO_ROOT = Path(__file__).resolve().parent.parent
CITIES_1000 = REPO_ROOT / "cities1000.txt"
DUMPS = [(1_000, REPO_ROOT / "cities1000.txt"), (5_000, REPO_ROOT / "cities5000.txt"),
         (15_000, REPO_ROOT / "cities15000.txt")]


def coordinates_for(position: int) -> tuple[float, float]:
    """A place's own corner of the globe: far enough apart that no two pairs share a distance
    by accident, and inside the range the dumps are checked against however many places a
    fixture holds."""
    return (-60.0 + position * 0.5, -120.0 + position * 0.25)


def database_of_names(
    directory: Path,
    names: Sequence[str],
    *,
    populations: Sequence[int] | None = None,
    default_population: int = 10_000,
    dump_name: str = "cities2.txt",
) -> sqlite3.Connection:
    """An imported database holding exactly these names, in this order.

    Populations default to one value so a floor test can say which places it expects to
    disappear; coordinates are derived from the position, so two places never share them.
    """
    dump = write_dump(
        directory / dump_name,
        [
            dump_row(
                position + 1,
                name,
                *coordinates_for(position),
                population=(
                    default_population if populations is None else populations[position]
                ),
            )
            for position, name in enumerate(names)
        ],
    )
    return import_dump(directory / "places.sqlite3", dump)


def ids(places: Iterable[Place]) -> list[int]:
    return [place.geonameid for place in places]


def signature(matched: object) -> tuple[int, int, float]:
    pair: MatchedPair = matched  # type: ignore[assignment]
    return (pair.first.geonameid, pair.second.geonameid, pair.distance_km)


def signatures(matcheds: Iterable[object]) -> list[tuple[int, int, float]]:
    return [signature(matched) for matched in matcheds]


def sorted_signatures(matcheds: Iterable[object]) -> list[tuple[int, int, float]]:
    return sorted(signatures(matcheds))


def unordered_signature(matched: object) -> tuple[tuple[int, int], float]:
    """A pair without a direction: which two places, and how far apart.

    Which place a path calls `first` follows its own candidate order - a dump lists the
    biggest places first, the database walks them smallest first - so between the two paths
    a pair is only comparable once the direction is dropped.
    `test_a_pair_is_oriented_by_the_candidate_order` pins the directions themselves.
    """
    pair: MatchedPair = matched  # type: ignore[assignment]
    return (
        (min(pair.first.geonameid, pair.second.geonameid), max(pair.first.geonameid, pair.second.geonameid)),
        pair.distance_km,
    )


def unordered_signatures(matcheds: Iterable[object]) -> list[tuple[tuple[int, int], float]]:
    return sorted(unordered_signature(matched) for matched in matcheds)


class TestTheFloorChoosesTheCandidates:
    NAMES = ["Aba", "Abe", "Acy", "Ade"]
    POPULATIONS = [4_999, 5_000, 5_001, 6_000]

    @pytest.fixture
    def database(self, tmp_path: Path) -> sqlite3.Connection:
        return database_of_names(tmp_path, self.NAMES, populations=self.POPULATIONS)

    @pytest.mark.parametrize(
        ("floor", "expected"),
        [
            (0, [1, 2, 3, 4]),
            (4_999, [1, 2, 3, 4]),
            (5_000, [2, 3, 4]),
            (5_001, [3, 4]),
            (6_000, [4]),
            (6_001, []),
        ],
    )
    def test_the_floor_decides_which_places_are_compared(
        self, database: sqlite3.Connection, floor: int, expected: list[int]
    ) -> None:
        assert ids(candidate_places(database, floor)) == expected

    def test_a_place_exactly_on_the_floor_is_compared(self, tmp_path: Path) -> None:
        """The boundary belongs to the search: a place at population 5000 is a candidate at a
        floor of 5000, and the pair it forms is reported."""
        database = database_of_names(tmp_path, ["Aba", "Abe"], populations=[5_000, 6_000])

        pairs = list(scan_database(database, 1, 5_000))

        assert [(pair.first.geonameid, pair.second.geonameid) for pair in pairs] == [(1, 2)]
        assert pairs[0].distance_km == pytest.approx(
            great_circle_distance_km(*coordinates_for(0), *coordinates_for(1))
        )
        assert signatures(scan_database(database, 1, 5_001)) == [], (
            "lift the floor by one person and Aba stops being a candidate, so the pair is "
            "never asked about rather than found and hidden"
        )

    def test_the_floor_shortens_the_search_rather_than_the_report(
        self, database: sqlite3.Connection
    ) -> None:
        unfiltered = scope_for(database)
        filtered = scope_for(database, 5_001)

        assert filtered.places == 2
        assert filtered.pair_count < unfiltered.pair_count, (
            "the work a search does is measured by pairs it compares, not pairs it displays"
        )
        found_unfiltered = sorted_signatures(scan_database(database, 1))
        found_filtered = sorted_signatures(scan_database(database, 1, 5_001))
        assert set(found_filtered) < set(found_unfiltered), (
            "a floor removes pairs; it cannot invent one"
        )

    def test_nothing_below_the_floor_is_even_asked_about(self, database: sqlite3.Connection) -> None:
        floor = 5_000
        candidates = set(ids(candidate_places(database, floor)))
        compared = {
            geonameid
            for pair_first, pair_second, _ in signatures(scan_database(database, 2, floor))
            for geonameid in (pair_first, pair_second)
        }
        assert compared <= candidates, (
            f"the scan compared {sorted(compared - candidates)}, below the floor of {floor}"
        )

    def test_no_floor_is_every_place_and_the_effective_floor_says_so(
        self, database: sqlite3.Connection
    ) -> None:
        everything = scope_for(database)
        assert everything.places == 4
        assert everything.population_floor == 4_999, "the table's own minimum is the real floor"
        assert ids(candidate_places(database)) == [1, 2, 3, 4]

        assert scope_for(database, 5_000).population_floor == 5_000

    @pytest.mark.parametrize(
        ("expressed_as", "threshold"),
        [
            ("cities1000.txt", 1_000),
            ("cities5000.txt", 5_000),
            ("cities15000.txt", 15_000),
            ("5000", 5_000),
            (15_000, 15_000),
        ],
    )
    def test_a_floor_named_as_a_dataset_threshold_is_the_same_floor(
        self, tmp_path: Path, expressed_as: str | int, threshold: int
    ) -> None:
        """The number in a dump name and the population floor are one number, so a caller can
        say "the cities5000 set" and mean the same search."""
        names = [f"Name{index}" for index in range(12)]
        populations = [500 * index for index in range(1, 13)]
        database = database_of_names(tmp_path, names, populations=populations)

        by_number = ids(candidate_places(database, dataset_threshold(expressed_as)))
        by_hand = ids(candidate_places(database, threshold))

        assert dataset_threshold(expressed_as) == threshold
        assert by_number == by_hand
        assert by_number == [
            index + 1 for index, population in enumerate(populations) if population >= threshold
        ]

    def test_the_floor_applies_inside_a_name_length_group_too(self, tmp_path: Path) -> None:
        database = database_of_names(
            tmp_path,
            ["Aba", "Abe", "Abba", "Abbu"],
            populations=[1_000, 6_000, 6_000, 500],
        )
        assert ids(candidate_places(database, 5_000, name_length=3)) == [2]
        assert ids(candidate_places(database, 5_000, name_length=4)) == [3]
        assert candidate_name_lengths(database, 5_000) == [3, 4]
        assert candidate_name_lengths(database, 6_001) == []


class TestCandidateOrderAndPaging:
    NAMES = ["Aba", "Abe", "Aca", "Ace"]
    POPULATIONS = [10, 10, 7, 3]

    @pytest.fixture
    def database(self, tmp_path: Path) -> sqlite3.Connection:
        return database_of_names(tmp_path, self.NAMES, populations=self.POPULATIONS)

    def test_candidates_come_back_in_one_stable_order(self, database) -> None:
        """Population ascending, GeoNames id to break a tie: an order the walk can page
        through, and one a repeated search cannot drift on."""
        assert ids(candidate_places(database)) == [4, 3, 1, 2]

    @pytest.mark.parametrize("page_size", [1, 2, 3, 1_000])
    def test_paging_changes_nothing(self, database, page_size: int) -> None:
        assert ids(candidate_places(database, page_size=page_size)) == [4, 3, 1, 2]
        assert ids(candidate_places(database, 7, page_size=page_size)) == [3, 1, 2]
        assert ids(candidate_places(database, name_length=3, page_size=page_size)) == [4, 3, 1, 2]

    def test_an_unusable_page_size_is_refused(self, database) -> None:
        with pytest.raises(ValueError, match="page_size"):
            list(candidate_places(database, page_size=0))

    def test_one_place_can_be_left_out_without_disturbing_the_rest(self, database) -> None:
        """Searching *from* a place means the place must not pair with itself, and must not
        drag the ordering around with it."""
        assert ids(candidate_places(database, exclude_geonameid=1)) == [4, 3, 2]
        assert ids(candidate_places(database, exclude_geonameid=4)) == [3, 1, 2]
        assert ids(candidate_places(database, exclude_geonameid=99)) == [4, 3, 1, 2]

    def test_a_name_length_group_holds_only_that_length(self, tmp_path: Path) -> None:
        database = database_of_names(tmp_path, NEAR_MISSES + ["Abba", "Abbott", "Z"])
        lengths = candidate_name_lengths(database)
        assert lengths == sorted(lengths)
        for length in lengths:
            group = list(candidate_places(database, name_length=length))
            assert group, f"{length} was offered as a length with no candidates"
            assert {len(place.asciiname) for place in group} == {length}

    def test_a_length_with_no_candidates_is_not_offered(self, database) -> None:
        assert 99 not in candidate_name_lengths(database)
        assert list(candidate_places(database, name_length=99)) == []


class TestTheDatabaseScanAgreesWithTheCoreScan:
    """Both candidate paths, against the rules frozen in `tests/test_matching_rules.py`.

    The database walk reports pairs group by group and population ascending, while a scan of
    a dump follows the dump's order, so these compare the pairs themselves - never their
    sequence. What must match exactly is which pairs, and the distance on each.
    """

    @pytest.mark.parametrize("names, character_difference", FIXTURE_CASES)
    def test_nothing_is_lost_and_nothing_is_gained(
        self, tmp_path: Path, names: list[str], character_difference: int
    ) -> None:
        dump = write_dump(
            tmp_path / "cities2.txt",
            [
                dump_row(position + 1, name, *coordinates_for(position))
                for position, name in enumerate(names)
            ],
        )
        database = import_dump(tmp_path / "places.sqlite3", dump)
        places = read_places(dump)

        from_file = sorted_signatures(scan(places, character_difference))
        grouped = sorted_signatures(scan_database(database, character_difference))
        compared = sorted_signatures(
            scan_database(database, character_difference, equal_length_prefilter=False)
        )

        assert grouped == from_file, "the database lost or reordered a pair"
        assert compared == from_file, "the walk without the length groups disagreed"

    @pytest.mark.parametrize("floor", [None, 0, 1, 5_000, 10_000, 90_000])
    def test_the_floor_means_the_same_thing_on_both_paths(self, tmp_path: Path, floor: int | None) -> None:
        names = [f"Name{index}" for index in range(9)]
        populations = [index * 1_000 for index in range(9)]
        dump = write_dump(
            tmp_path / "cities2.txt",
            [
                dump_row(
                    position + 1,
                    name,
                    *coordinates_for(position),
                    population=population,
                )
                for position, (name, population) in enumerate(zip(names, populations))
            ],
        )
        database = import_dump(tmp_path / "places.sqlite3", dump)

        from_file = sorted_signatures(scan(read_places(dump), 4, floor))
        from_database = sorted_signatures(scan_database(database, 4, floor))

        assert from_database == from_file
        assert from_database == sorted_signatures(
            scan_database(database, 4, floor, equal_length_prefilter=False)
        )

    def test_a_pair_is_oriented_by_the_candidate_order(self, tmp_path: Path) -> None:
        """The one way the two paths are allowed to differ: which half of a pair is `first`.

        A dump lists places by decreasing population, so the legacy pair names the bigger
        place first. The database walks candidates in increasing population, so its pair
        names the smaller one first. Same pair, same distance, opposite orientation - which
        is why the real-data comparisons below match pairs without a direction.
        """
        dump = write_dump(
            tmp_path / "cities2.txt",
            [
                dump_row(1, "Aba", *coordinates_for(0), population=9_000),
                dump_row(2, "Abe", *coordinates_for(1), population=2_000),
            ],
        )
        database = import_dump(tmp_path / "places.sqlite3", dump)

        from_file = [(p.first.geonameid, p.second.geonameid) for p in scan(read_places(dump), 1)]
        from_database = [(p.first.geonameid, p.second.geonameid) for p in scan_database(database, 1)]

        assert from_file == [(1, 2)], "the dump lists the bigger place first"
        assert from_database == [(2, 1)], "the database lists the smaller place first"

    def test_a_search_is_repeatable(self, tmp_path: Path) -> None:
        """Same pairs, same order, same distances, however the walk is paged: the candidate
        order is a promise, not an accident of the query planner."""
        database = database_of_names(
            tmp_path,
            NEAR_MISSES + ["Abba", "Aboy"],
            populations=[9_000, 8_000, 7_000, 6_000, 5_000, 4_000, 3_000],
        )
        first = signatures(scan_database(database, 1, 1_000))
        assert first, "the fixture has to produce a pair for this to mean anything"
        assert signatures(scan_database(database, 1, 1_000)) == first
        assert signatures(scan_database(database, 1, 1_000, page_size=1)) == first
        assert signatures(scan_database(database, 1, 1_000, page_size=10_000)) == first

    @pytest.mark.parametrize("character_difference", [0, 1])
    def test_a_place_is_a_pair_with_itself(self, tmp_path: Path, character_difference: int) -> None:
        """Threshold 0 reports each candidate once, at a distance of nothing - the behaviour
        the original tool had, kept on purpose."""
        database = database_of_names(tmp_path, ["Aba", "Abe", "Abu"], populations=[5_000, 6_000, 100])
        pairs = list(scan_database(database, 0, 5_000))
        assert [(pair.first.geonameid, pair.second.geonameid) for pair in pairs] == [(1, 1), (2, 2)], (
            "candidates come back population ascending, so Aba at exactly 5000 comes first"
        )
        assert all(pair.distance_km == 0.0 for pair in pairs)
        assert all(pair.first.geonameid == pair.second.geonameid for pair in pairs)

    def test_a_place_left_out_has_no_pair_with_itself(self, tmp_path: Path) -> None:
        database = database_of_names(tmp_path, ["Aba", "Abe"])
        assert [(p.first.geonameid, p.second.geonameid) for p in scan_database(database, 0)] == [
            (1, 1),
            (2, 2),
        ]
        assert [(p.first.geonameid, p.second.geonameid) for p in scan_database(
            database, 0, exclude_geonameid=1
        )] == [(2, 2)]


class TestAnEmptyDatabaseSaysSo:
    def test_scanning_an_unimported_database_names_the_command_that_fixes_it(
        self, tmp_path: Path
    ) -> None:
        database = connect(tmp_path / "places.sqlite3")
        with pytest.raises(DatabaseNotImportedError, match="distnsimilarity-import"):
            list(scan_database(database, 1))

    def test_an_empty_database_has_a_scope_of_nothing(self, tmp_path: Path) -> None:
        database = connect(tmp_path / "places.sqlite3")
        scope = scope_for(database)
        assert scope.places == 0
        assert scope.pair_count == 0
        assert scope.full_pair_count == 0
        assert scope.skipped_share == 0.0
        assert candidate_name_lengths(database) == []


class TestTheScopeOfASearch:
    NAMES = ["Aba", "Abe", "Aec", "Abba", "Taraa"]

    @pytest.fixture
    def database(self, tmp_path: Path) -> sqlite3.Connection:
        return database_of_names(tmp_path, self.NAMES)

    def test_the_scope_counts_what_is_about_to_be_compared(self, database) -> None:
        scope = scope_for(database, 10_000)

        assert scope.places == 5
        assert scope.population_floor == 10_000
        assert [(group.name_length, group.places) for group in scope.groups] == [(3, 3), (4, 1), (5, 1)]
        assert scope.pair_count == 8, "3 names of 3 characters is 6 pairs, then 1 and 1"
        assert scope.full_pair_count == 15, "5 places compared against themselves and what follows"
        assert scope.skipped_share == pytest.approx(1 - 8 / 15)

    def test_the_summary_is_said_in_numbers_a_person_can_check(self, database) -> None:
        summary = scope_for(database, 10_000).summary()
        assert "5 place(s) at population >= 10000" in summary
        assert "8 pair(s) to compare" in summary
        assert "instead of 15" in summary

    def test_a_floor_narrows_the_scope_before_the_search_starts(self, database) -> None:
        scope = scope_for(database, 20_000)
        assert scope.places == 0
        assert scope.pair_count == 0


class TestTwoPlacesCanShareAName:
    def test_a_name_is_a_question_not_an_answer(self, tmp_path: Path) -> None:
        """Searching from a place by name has to offer the choices rather than guess: Lima is
        in Peru and Ohio, and the distance between them is the interesting part."""
        dump = write_dump(
            tmp_path / "cities2.txt",
            [
                dump_row(50, "Lima", -12.05, -77.04, countrycode="PE", population=8_012_109),
                dump_row(51, "Lima", 40.57, -83.62, countrycode="US", population=42_954),
                dump_row(52, "Lim a", 1.0, 2.0),
            ],
        )
        database = import_dump(tmp_path / "places.sqlite3", dump)

        named = places_with_ascii_name(database, "Lima")
        assert [place.geonameid for place in named] == [50, 51]
        assert [place.country_code for place in named] == ["PE", "US"]
        assert places_with_ascii_name(database, "lima") == []
        assert places_with_ascii_name(database, "") == []


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory):
    """The three committed dumps, imported once into a temporary database for the slow tests
    that need a real one. Nothing here writes inside the repository."""
    directory = tmp_path_factory.mktemp("real-database")
    database = directory / "places.sqlite3"
    connection = connect(database)
    report = import_places(connection, DUMPS)
    yield connection, report
    connection.close()


@pytest.mark.slow
class TestTheRealDatabasesScope:
    """The committed dumps: 230,160 rows in, 146,782 places out, and the candidate walk uses
    the indexes on a table big enough for the planner to mean it."""

    def test_the_nested_dumps_deduplicate_to_one_row_per_place(self, imported) -> None:
        connection, report = imported

        assert report.input_rows == 230_160, "146782 + 55700 + 27678 rows read"
        assert report.places == 146_782, "and one place per GeoNames id, no more"
        assert report.deduplicated == 83_378
        assert report.unreadable == (), "the current dumps refuse nothing: " + ", ".join(
            report.problem_lines()
        )
        assert report.inserted == 146_782
        assert connection.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT geonameid) FROM place"
        ).fetchone()[0] == 0

    def test_every_place_is_recorded_by_the_most_inclusive_dump(self, imported) -> None:
        connection, _ = imported
        assert [
            (int(row["source_threshold"]), int(row["places"]))
            for row in connection.execute(
                "SELECT source_threshold, COUNT(*) AS places FROM place "
                "GROUP BY source_threshold ORDER BY source_threshold"
            )
        ] == [(1_000, 146_782)], (
            "cities5000.txt and cities15000.txt are subsets of cities1000.txt, so every place "
            "is recorded by the smallest threshold that holds it"
        )

    def test_importing_only_the_narrower_dumps_moves_the_record(self, tmp_path: Path) -> None:
        """Ask for the two smaller dumps and the same places are recorded at 5000: the field
        says which dumps were available, not which ones exist."""
        connection = connect(tmp_path / "places.sqlite3")
        report = import_places(connection, DUMPS[1:])
        assert report.places == 55_700
        assert [
            int(row["source_threshold"])
            for row in connection.execute(
                "SELECT source_threshold FROM place GROUP BY source_threshold"
            )
        ] == [5_000]

    def test_the_queries_both_search_modes_run_are_index_lookups(self, imported) -> None:
        from distnsimilarity.db.schema import query_plan

        connection, _ = imported
        plans = {
            "everywhere mode walks a name length": (
                "SELECT geonameid, asciiname, display_name, latitude, longitude, population, "
                "country_code FROM place WHERE name_length = ? AND population >= ? "
                "ORDER BY population, geonameid LIMIT 2000",
                (8, 5_000),
            ),
            "from-a-place mode walks a name length without itself": (
                "SELECT geonameid FROM place WHERE name_length = ? AND population >= ? "
                "AND geonameid <> ? ORDER BY population, geonameid",
                (8, 5_000, 3040051),
            ),
            "the cost estimate counts the groups": (
                "SELECT name_length, COUNT(*) FROM place WHERE population >= ? "
                "GROUP BY name_length ORDER BY name_length",
                (5_000,),
            ),
            "the name lookup behind disambiguation": (
                "SELECT geonameid FROM place WHERE asciiname = ? ORDER BY geonameid",
                ("Lima",),
            ),
        }
        for label, (statement, parameters) in plans.items():
            plan = query_plan(connection, statement, parameters)
            assert "idx_place" in plan, f"{label} degenerated into a table scan: {plan}"
            assert "SCAN place" not in plan or "COVERING INDEX" in plan, (
                f"{label} walked the table: {plan}"
            )

    def test_the_scan_of_real_places_agrees_with_the_scan_of_the_dump(
        self, tmp_path: Path
    ) -> None:
        """Places 600 to 1200 of `cities1000.txt`, imported and searched both ways. A slice,
        because the point is agreement rather than waiting on 146,782 places."""
        places = read_places(CITIES_1000)[600:1_200]
        dump = write_dump(
            tmp_path / "cities1000.txt",
            [
                dump_row(
                    place.geonameid,
                    place.asciiname,
                    place.latitude,
                    place.longitude,
                    name=place.display_name,
                    population=place.population,
                    countrycode=place.country_code,
                )
                for place in places
            ],
        )
        database = import_dump(tmp_path / "slice.sqlite3", dump)

        for character_difference in (0, 1, 2):
            from_file = unordered_signatures(scan(places, character_difference))
            assert from_file or character_difference == 0
            assert unordered_signatures(
                scan_database(database, character_difference, page_size=200)
            ) == from_file, f"the database disagreed at n={character_difference}"
            assert unordered_signatures(
                scan_database(database, character_difference, equal_length_prefilter=False)
            ) == from_file, f"the walk without length groups disagreed at n={character_difference}"
            assert len({pair for pair, _ in from_file}) == len(from_file), (
                "no pair twice, in either direction"
            )

    def test_a_floor_selects_the_places_the_dumps_say_it_does(self, imported) -> None:
        expected = {
            place.geonameid for place in read_places(CITIES_1000) if place.population >= 5_000
        }
        connection, _ = imported

        selected = {place.geonameid for place in candidate_places(connection, 5_000)}
        scope = scope_for(connection, 5_000)

        assert selected == expected, (
            f"a floor of 5000 has to select the {len(expected)} places the dumps put there"
        )
        assert scope.places == len(expected)
        assert len(expected) < 146_782, "the floor has to actually exclude something to mean anything"
        assert connection.execute(
            "SELECT COUNT(*) FROM place WHERE population = 5000"
        ).fetchone()[0] == 86, "86 places sit exactly on the boundary, and all 86 are candidates"

    def test_the_scope_of_a_floor_is_measured_from_the_table(self, imported) -> None:
        from distnsimilarity.core.prefilter import all_pair_count

        connection, _ = imported
        by_name_length: dict[int, int] = {}
        for place in read_places(CITIES_1000):
            if place.population >= 5_000:
                by_name_length[len(place.asciiname)] = by_name_length.get(len(place.asciiname), 0) + 1

        scope = scope_for(connection, 5_000)

        assert [(group.name_length, group.places) for group in scope.groups] == sorted(
            by_name_length.items()
        ), "the table groups the same places the dump does, the same way"
        assert scope.places == sum(by_name_length.values())
        assert scope.pair_count == sum(
            all_pair_count(count) for count in by_name_length.values()
        )
        assert scope.pair_count < scope.full_pair_count
        assert scope.skipped_share > 0.9, (
            "on the real set the length groups skip most of the work: "
            f"{scope.skipped_share:.1%}"
        )
