"""The search seam: one way to open a search, one way to stop a running one.

Issue #6 put both adapters - the command line and the HTTP API - behind `open_search`, so
this is where the things they share are pinned: which pairs come out, in which order, what
a bad request costs, and what a cancelled search leaves behind. The adapter tests only check
that their adapter passes this through.

The search source is the place database, so every fixture here is a database:
`place_database` in `tests/support.py` writes rows to a throwaway dump and runs the real import
over them.

The cancellation tests need a scan long enough to interrupt, which is what `noisy_database` is
for: a planted pair first, then a long stretch of equal-length names that match nothing.
Everything else runs on hand-checkable fixtures.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from distnsimilarity.core.matching import differing_position_indexes
from distnsimilarity.core.scan import scan
from distnsimilarity.core.topk import DEFAULT_TOP_K, Ranking, snapshots
from distnsimilarity.datasets import DatasetNotAvailableError, WHOLE_DATABASE_LABEL
from distnsimilarity.db.candidates import candidate_places, dataset_options
from distnsimilarity.db.schema import connect
from distnsimilarity.search import (
    CacheNotImportedError,
    InvalidSearchRequest,
    MeasuredRate,
    SearchControl,
    SearchRequest,
    open_search,
)
from support import (
    NOISY_CHARACTER_DIFFERENCE,
    NOISY_DUMP_PAIR,
    dump_row,
    noisy_database,
    place_database,
)


def two_pair_database(path: Path) -> Path:
    """Six places, three pairs at `n=1`, in an order a reader can check by eye.

    Two pairs share a name, so the order pairs come out in says something about the scan:
    `alpha` is a place, and a place is a pair with itself and the later places.
    """
    rows = [
        dump_row(11, "alpha", 40.0, -100.0, population=90_000),
        dump_row(12, "aleha", 41.0, -101.0, population=80_000),
        dump_row(13, "aloha", 42.0, -102.0, population=70_000),
        dump_row(14, "brant", 43.0, -103.0, population=60_000),
        dump_row(15, "brand", 44.0, -104.0, population=50_000),
        dump_row(16, "braid", 45.0, -105.0, population=40_000),
    ]
    return place_database(path, rows)


def pairs_of(request: SearchRequest) -> list[tuple[str, str, float]]:
    """Every pair a search reports, as `(name1, name2, distance)` - the shape tests compare."""
    session = open_search(request)
    return [(m.first.asciiname, m.second.asciiname, m.distance_km) for m in session.pairs]


class TestOpeningASearch:
    def test_a_search_says_what_it_covers_and_how_many_places_it_holds(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1, dataset="5000"))

        assert session.name == "cities of 5,000 or more people", (
            "a search says what it covers in the words a person chooses a dataset with, not as "
            f"a file name: {session.name!r}"
        )
        assert session.stats.places == 6, (
            f"the search reported {session.stats.places} places for a 6-row database"
        )
        assert session.stats.dataset_threshold == 5_000
        assert session.stats.database_path.endswith("places.sqlite3")

    def test_the_pairs_come_in_the_order_the_core_scan_reports_them(
        self, tmp_path: Path
    ) -> None:
        """The seam adds no ordering of its own.

        The reference is the core scan over the same candidates in the order the database hands
        them out - population ascending - which is the order the two can be compared in.
        """
        database = two_pair_database(tmp_path / "places.sqlite3")
        request = SearchRequest(database=database, n=1)

        through_the_seam = pairs_of(request)
        connection = connect(database)
        straight_off_the_scan = [
            (m.first.asciiname, m.second.asciiname, m.distance_km)
            for m in scan(list(candidate_places(connection, None)), 1)
        ]
        connection.close()

        assert through_the_seam == straight_off_the_scan, (
            "the seam and the scan disagree about which pairs or what order - through the "
            f"seam {through_the_seam}, straight off the scan {straight_off_the_scan}"
        )

    def test_a_dataset_that_names_no_threshold_is_refused(self, tmp_path: Path) -> None:
        """A dataset is a population rule, so the spellings that are not one are refused."""
        database = two_pair_database(tmp_path / "places.sqlite3")
        for not_a_dataset in ("   ", str(database), "../dumps/cities5000.txt"):
            with pytest.raises(InvalidSearchRequest) as refused:
                SearchRequest(database=database, n=1, dataset=not_a_dataset)
            assert "dataset" in str(refused.value), (
                f"refusing {not_a_dataset!r} did not say so: {refused.value}"
            )

    def test_a_dataset_this_database_cannot_honour_names_the_choices_that_exist(
        self, tmp_path: Path
    ) -> None:
        """99,000 is a rule no place here is under, so it is not a dataset of this database.

        An empty result would be a reportable answer, but a useless one: nobody asks to search
        only the cities of 99,000 or more people in a cache of places that top out lower. The
        refusal answers the question that was actually being asked, which is what is searchable
        here.
        """
        database = two_pair_database(tmp_path / "places.sqlite3")

        with pytest.raises(DatasetNotAvailableError) as refused:
            open_search(SearchRequest(database=database, n=1, dataset="99000"))

        message = str(refused.value)
        assert "99,000" in message, message
        assert "every place in the database" in message, (
            f"the refusal has to name what this database can be searched as: {message}"
        )

    def test_a_negative_character_difference_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSearchRequest) as refused:
            SearchRequest(database=two_pair_database(tmp_path / "places.sqlite3"), n=-1)
        assert "cannot be negative" in str(refused.value)

    def test_a_negative_population_floor_is_refused(self) -> None:
        with pytest.raises(InvalidSearchRequest):
            SearchRequest(dataset="1000", n=1, min_population=-5)

    def test_the_population_floor_decides_what_is_compared_not_what_is_displayed(
        self, tmp_path: Path
    ) -> None:
        rows = [
            dump_row(1, "alpha", 40.0, -100.0, population=90_000),
            dump_row(2, "aleha", 41.0, -101.0, population=80_000),
            dump_row(3, "aloha", 42.0, -102.0, population=10),
        ]
        database = place_database(tmp_path / "places.sqlite3", rows)
        request = SearchRequest(database=database, n=1)

        without_a_floor = pairs_of(request)
        with_a_floor = pairs_of(SearchRequest(database=database, n=1, min_population=1_000))

        assert [name for first, second, _ in without_a_floor for name in (first, second)].count(
            "aloha"
        ) == 2, "at no floor, the 10-person place is a partner twice"
        assert all("aloha" not in (first, second) for first, second, _ in with_a_floor), (
            "a place below the floor was still compared"
        )
        assert len(with_a_floor) == 1

    def test_a_full_comparison_reports_the_same_pairs_in_the_same_order(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        with_prefilter = pairs_of(SearchRequest(database=database, n=1))
        without_prefilter = pairs_of(
            SearchRequest(database=database, n=1, equal_length_prefilter=False)
        )
        assert with_prefilter == without_prefilter

    def test_the_dataset_choice_narrows_what_is_searched(self, tmp_path: Path) -> None:
        """A dataset is a filter, not a label: it changes which pairs are compared.

        The fixture holds two matching pairs. One sits above 50,000 people and one below, so a
        search that chooses `cities of 50,000 or more people` reports one of them - which is what
        `cities50000.txt` would have been, now that the dumps are one table.
        """
        database = mixed_length_database(tmp_path / "places.sqlite3")
        everything = pairs_of(SearchRequest(database=database, n=1))
        at_50_000 = SearchRequest(database=database, n=1, dataset="50000")

        assert {frozenset(pair[:2]) for pair in everything} == {
            frozenset(("abel", "abed")),
            frozenset(("tabs", "tobs")),
            frozenset(("tar", "tor")),
        }
        assert {frozenset(pair[:2]) for pair in pairs_of(at_50_000)} == {
            frozenset(("abel", "abed"))
        }, "choosing a dataset left a pair from below it in the results"

        session = open_search(at_50_000)
        everything_session = open_search(SearchRequest(database=database, n=1))
        assert session.stats.places == 5, "five of the nine places have 50,000 people or more"
        assert session.stats.dataset_threshold == 50_000
        assert session.stats.candidate_pairs < everything_session.stats.candidate_pairs, (
            "the dataset did not reduce the work"
        )

    def test_the_choices_are_built_from_the_data_and_each_searches_different_places(
        self, tmp_path: Path
    ) -> None:
        """The dropdown is read out of the cache, and a choice that changes nothing is no choice.

        `cities of 1,000 or more people` is the interesting row: the places here are on both
        sides of every threshold, so all four rules mean different searches, and the whole cache
        is listed by what it is rather than by how many rows it happens to hold today.
        """
        database = spread_database(tmp_path / "places.sqlite3")
        connection = connect(database)
        try:
            options = dataset_options(connection)
        finally:
            connection.close()

        assert [option.threshold for option in options] == [None, 1_000, 5_000, 15_000]
        assert [option.label for option in options] == [
            "every place in the database",
            "cities of 1,000 or more people",
            "cities of 5,000 or more people",
            "cities of 15,000 or more people",
        ], "a dataset is worded as a rule about people, never as a bare number or a file name"
        assert [option.places for option in options] == [8, 5, 4, 2]
        assert len({option.candidate_pairs for option in options}) == len(options), (
            "every choice on the list has to be a different amount of work"
        )

    def test_the_words_of_a_choice_are_the_rule_the_search_runs(self, tmp_path: Path) -> None:
        """A label that drifts from its threshold is a lie in a dropdown.

        Each choice is checked against a search made with that number: the digits in the words
        are the threshold the filter ran at, the words the search reports back are the words in
        the list, and no choice is a bare number, a file name, or a count that will go stale.
        """
        database = spread_database(tmp_path / "places.sqlite3")
        connection = connect(database)
        try:
            options = dataset_options(connection)
        finally:
            connection.close()

        assert options, "a cache with places in it offers something"
        for option in options:
            if option.threshold is None:
                assert option.label == WHOLE_DATABASE_LABEL
                continue
            assert f"{option.threshold:,}" in option.label, (
                f"the words lost the rule, or moved off it: {option.label!r} is not {option.threshold:,}"
            )
            assert option.label != str(option.threshold), f"a bare number is not a label: {option.label}"
            assert f"cities{option.threshold}" not in option.label.replace(",", ""), (
                f"a dataset is not a file name: {option.label}"
            )

            session = open_search(
                SearchRequest(database=database, n=1, dataset=str(option.threshold))
            )
            assert session.stats.dataset_threshold == option.threshold, (
                "the number in the words is not the number the search filtered on"
            )
            assert session.stats.dataset_label == option.label, (
                "the dropdown and the running search describe the same rule in different words"
            )

    def test_a_choice_that_would_search_the_same_places_as_another_is_not_offered(
        self, tmp_path: Path
    ) -> None:
        """A cache imported from `cities1000` alone has one dataset: everything in it.

        Every place in that dump has 1,000 people or more, so `cities of 1,000 or more people`
        would be the whole cache wearing a number, and every place here is a city of 40,000 or
        more, so the bigger thresholds hold the same places too.
        """
        database = two_pair_database(tmp_path / "places.sqlite3")
        connection = connect(database)
        try:
            options = dataset_options(connection)
        finally:
            connection.close()

        assert [option.threshold for option in options] == [None]
        assert [option.label for option in options] == ["every place in the database"]

    def test_a_higher_threshold_searches_fewer_places_and_finds_fewer_pairs(
        self, tmp_path: Path
    ) -> None:
        database = spread_database(tmp_path / "places.sqlite3")

        found: dict[str | None, int] = {}
        costs: dict[str | None, int] = {}
        for threshold in (None, "1000", "5000", "15000"):
            session = open_search(SearchRequest(database=database, n=1, dataset=threshold))
            costs[threshold] = session.stats.candidate_pairs
            found[threshold] = len(list(session.pairs))

        assert [found[key] for key in (None, "1000", "5000", "15000")] == [3, 2, 2, 1], (
            "a dataset is a filter on the answers, not a label on the same ones: the villages "
            "are out above 1,000 and only the big city pair survives 15,000"
        )
        assert costs[None] > costs["1000"] > costs["5000"] > costs["15000"], (
            f"the work did not fall with the places: {costs}"
        )

    def test_the_dataset_and_the_population_floor_together_mean_the_stricter(
        self, tmp_path: Path
    ) -> None:
        """Both are floors on candidate population, so a request giving both gets the higher.

        Neither is quietly dropped: a 60,000 dataset with a 40,000 floor is still 60,000, and a
        70,000 floor under a 60,000 dataset is 70,000.
        """
        database = mixed_length_database(tmp_path / "places.sqlite3")
        dataset_only = open_search(SearchRequest(database=database, n=1, dataset="60000"))
        lower_floor = open_search(
            SearchRequest(database=database, n=1, dataset="60000", min_population=40_000)
        )
        higher_floor = open_search(
            SearchRequest(database=database, n=1, dataset="60000", min_population=70_000)
        )

        assert dataset_only.stats.places == lower_floor.stats.places == 4
        assert higher_floor.stats.places == 3

    def test_a_database_with_no_places_in_it_is_refused_by_name(self, tmp_path: Path) -> None:
        empty = tmp_path / "places.sqlite3"
        connect(empty).close()

        with pytest.raises(CacheNotImportedError) as refused:
            open_search(SearchRequest(n=1, database=empty))
        assert "distnsimilarity-import" in str(refused.value), (
            "the refusal has to say what to do about it"
        )
        assert "only search source" in str(refused.value), (
            "and it has to say there is nothing else to fall back to"
        )

    def test_a_database_that_is_not_there_is_refused_rather_than_searched_around(
        self, tmp_path: Path
    ) -> None:
        """A wrong path is a wrong path: the answer is the import command, not other data."""
        missing = tmp_path / "nowhere" / "places.sqlite3"

        with pytest.raises(CacheNotImportedError) as refused:
            open_search(SearchRequest(n=1, database=missing))
        assert str(missing) in str(refused.value)


class TestStoppingASearch:
    def test_a_search_that_is_left_alone_reports_everything_it_found(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=300)
        session = open_search(SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE))
        pairs = list(session.pairs)

        assert session.stats.completed is True
        assert session.stats.cancelled is False
        assert session.stats.pairs_emitted == len(pairs) == 1, (
            f"the noisy fixture was supposed to hold exactly the planted pair, found {pairs}"
        )
        assert session.stats.comparison_polls > 0, "the scan polled its cancellation check"

    def test_a_reader_that_stops_gets_exactly_the_pairs_before_that_point(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=1_500)
        request = SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE)
        whole = pairs_of(request)

        session = open_search(request)
        seen: list[tuple[str, str, float]] = []
        for index, match in enumerate(session.pairs):
            seen.append((match.first.asciiname, match.second.asciiname, match.distance_km))
            if index == 0:
                session.control.cancel("the reader stopped reading")
        seen.extend(
            (m.first.asciiname, m.second.asciiname, m.distance_km) for m in session.pairs
        )

        assert seen == whole[:1], (
            "a search stopped after 1 pair gave a different result than the first pair of a "
            f"full search: {seen} against {whole[:1]}"
        )
        assert session.stats.completed is False
        assert session.stats.cancelled is True
        assert session.stats.cancel_reason == "the reader stopped reading"

    def test_the_scan_stops_at_the_cancellation_rather_than_running_on(
        self, tmp_path: Path
    ) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=1_500)
        request = SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE)

        full = open_search(request)
        list(full.pairs)
        stopped = open_search(request)
        next(stopped.pairs)
        stopped.control.cancel("stopped after the first pair")
        assert list(stopped.pairs) == []

        assert stopped.stats.comparison_polls < full.stats.comparison_polls / 2, (
            f"a search stopped after one pair polled {stopped.stats.comparison_polls} times "
            f"against {full.stats.comparison_polls} for the whole scan - the scan kept going"
        )

    def test_a_search_cancelled_before_the_first_pair_reports_nothing(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1))
        session.control.cancel("nobody wanted the results")

        assert list(session.pairs) == []
        assert session.stats.pairs_emitted == 0
        assert session.stats.completed is False

    def test_cancelling_a_search_that_is_already_over_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1))
        pairs = list(session.pairs)
        session.control.cancel("too late")

        assert session.stats.completed is True
        assert session.stats.cancelled is False, (
            "a finished search is reported as finished, whatever happens afterwards"
        )
        assert session.stats.pairs_emitted == len(pairs)

    def test_the_first_reason_a_search_was_stopped_is_the_one_kept(self) -> None:
        control = SearchControl()
        control.cancel("the client hung up")
        control.cancel("the operator pressed stop")

        assert control.cancelled is True
        assert control.reason == "the client hung up"


class TestTheControlIsShared:
    def test_a_control_handed_in_from_outside_is_the_one_the_search_stops_for(
        self, tmp_path: Path
    ) -> None:
        """This is how an adapter stops a search it did not start: the command line's Ctrl-C
        handler, and later a control endpoint, both work this way."""
        database = noisy_database(tmp_path / "places.sqlite3", places=1_500)
        control = SearchControl(poll_every_comparisons=256)
        session = open_search(
            SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE), control=control
        )
        next(session.pairs)
        control.cancel("stopped from outside")

        assert list(session.pairs) == []
        assert control.polls > 0

    def test_a_scan_polls_its_check_once_per_poll_interval(self, tmp_path: Path) -> None:
        """The poll interval is the difference between stopping in a millisecond and stopping
        after the next few million comparisons, so its arithmetic is pinned here."""
        database = noisy_database(tmp_path / "places.sqlite3", places=400)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE), control=control
        )
        list(session.pairs)

        comparisons = 400 * 399 // 2
        # Every partner list polls once before its first comparison, so the true count sits
        # between one poll per comparison and one per interval.
        assert comparisons // 64 <= control.polls <= comparisons + 400, (
            f"{control.polls} polls for {comparisons} comparisons at an interval of 64"
        )


#: How long a seam test waits for something another thread does.
WAIT = 10.0


class TestPacingASearch:
    """Holding a scan at a poll boundary: the seam a driver uses when it cannot take a whole
    scan at once.

    The HTTP adapter writes a search to a socket, so it must be able to stop the scan where it
    is and hand over what it found - otherwise a rare-match scan is one indivisible block of
    work with no partial answers and no way to stop it. This is that seam, tested with nothing
    but the core: one thread pulls, the other is the driver that holds it.
    """

    def test_a_held_search_makes_no_more_comparisons_until_the_driver_lets_it_go(
        self, tmp_path: Path
    ) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=600)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE), control=control
        )
        held = threading.Event()
        let_go = threading.Event()

        def pace() -> None:
            held.set()
            assert let_go.wait(WAIT), "the driver never let the search go"

        control.pacing = pace
        pulled: list[tuple[str, str]] = []
        worker = threading.Thread(
            target=lambda: pulled.extend(
                (m.first.asciiname, m.second.asciiname) for m in session.pairs
            ),
            name="held-scan-under-test",
            daemon=True,
        )
        worker.start()

        assert held.wait(WAIT), "the scan never reached a boundary its driver could hold"
        compared_at_first_look = session.stats.pairs_compared
        polls_at_first_look = control.polls
        assert compared_at_first_look > 0, "a held scan has still compared what it reported"
        # The driver is not asking for anything, so the scan waits rather than running ahead of
        # the reader that is holding it.
        time.sleep(0.05)
        assert session.stats.pairs_compared == compared_at_first_look, (
            "the scan ran on for "
            f"{session.stats.pairs_compared - compared_at_first_look} comparisons while held"
        )
        assert control.polls == polls_at_first_look, "the scan polled its way past the hold"

        let_go.set()
        worker.join(WAIT)
        assert not worker.is_alive(), "the scan never resumed once its driver let it go"
        assert session.stats.completed is True
        assert pulled == [NOISY_DUMP_PAIR], "being paced changed what the search reported"

    def test_a_cancelled_search_is_not_held_for_a_driver_that_has_gone_away(
        self, tmp_path: Path
    ) -> None:
        """A driver that hangs up never comes back for the next slice, so waiting for it would
        be a search that runs forever rather than one that stops."""
        database = noisy_database(tmp_path / "places.sqlite3", places=600)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE), control=control
        )
        held = threading.Event()

        def pace() -> None:
            # Never released on purpose: this is what it looks like when the reader has gone.
            held.set()

        control.pacing = pace
        worker = threading.Thread(
            target=lambda: list(session.pairs), name="abandoned-scan-under-test", daemon=True
        )
        worker.start()

        assert held.wait(WAIT), "the scan never reached a boundary its driver could hold"
        control.cancel("the client hung up")
        worker.join(WAIT)

        assert not worker.is_alive(), (
            "a cancelled search is still waiting at a poll boundary for a reader that went away"
        )
        assert session.stats.cancelled is True
        assert session.stats.completed is False


def spread_database(path: Path) -> Path:
    """Seven places spread across the dataset thresholds, with a matching pair on each shelf.

    Each pair sits at one size - both halves of a pair are the same kind of place - so raising
    the threshold takes whole pairs out of the search: three pairs in the whole cache, two above
    1,000, two above 5,000, one above 15,000. `village` sits between 1,000 and 5,000, the villages
    below 1,000 and `hamlet` below everything, so that each of the four choices covers a different
    number of places and none of the extras matches anything at n=1.
    """
    return place_database(
        path,
        [
            dump_row(41, "abelard", 44.0, -93.0, population=20_000),
            dump_row(42, "abelurd", 39.0, -89.0, population=19_000),
            dump_row(43, "toronto", 40.0, -90.0, population=8_000),
            dump_row(44, "taronto", 41.0, -91.0, population=7_000),
            dump_row(48, "village", 45.0, -95.0, population=3_000),
            dump_row(45, "brant", 42.0, -92.0, population=900),
            dump_row(46, "brand", 43.0, -93.0, population=800),
            dump_row(47, "hamlet", 44.0, -94.0, population=500),
        ],
    )


def mixed_length_database(path: Path) -> Path:
    """Nine places in three name lengths, so the length groups have something to skip."""
    rows = [
        dump_row(21, "abel", 10.0, 20.0, population=90_000),
        dump_row(22, "orb", 11.0, 21.0, population=80_000),
        dump_row(23, "abed", 12.0, 22.0, population=70_000),
        dump_row(24, "ab", 13.0, 23.0, population=60_000),
        dump_row(25, "tor", 14.0, 24.0, population=50_000),
        dump_row(26, "tar", 15.0, 25.0, population=40_000),
        dump_row(27, "abi", 16.0, 26.0, population=30_000),
        dump_row(28, "tabs", 17.0, 27.0, population=20_000),
        dump_row(29, "tobs", 18.0, 28.0, population=10_000),
    ]
    return place_database(path, rows)


class TestWhatTheSearchIsAboutToDo:
    """The cost of a search, said before it runs, in the same units as the work it does."""

    def test_a_search_counts_its_candidate_pairs_before_comparing_any_of_them(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1))

        assert session.stats.candidate_pairs == 21, "six places, each against itself and later"
        assert session.stats.pairs_compared == 0, "counting the job is not doing it"

    @pytest.mark.parametrize(
        ("prefilter", "expected"), [(True, 21), (False, 45)], ids=["grouped", "everything"]
    )
    def test_the_counted_candidates_are_exactly_the_pairs_the_scan_compares(
        self, tmp_path: Path, prefilter: bool, expected: int
    ) -> None:
        """The estimate and the work are counted by one rule, or a progress bar lies."""
        database = mixed_length_database(tmp_path / "places.sqlite3")
        session = open_search(
            SearchRequest(database=database, n=1, equal_length_prefilter=prefilter),
                    )
        list(session.pairs)

        assert session.stats.candidate_pairs == expected
        assert session.stats.pairs_compared == expected, "the scan compared what it promised"
        assert session.stats.share_compared() == 1.0

    def test_the_length_groups_reduce_the_work_without_reducing_the_pairs(
        self, tmp_path: Path
    ) -> None:
        database = mixed_length_database(tmp_path / "places.sqlite3")
        grouped = open_search(SearchRequest(database=database, n=1))
        everything = open_search(
            SearchRequest(database=database, n=1, equal_length_prefilter=False),
                    )

        assert grouped.stats.candidate_pairs < everything.stats.candidate_pairs == 45
        assert grouped.stats.candidate_pairs == 21, "one 2-letter, four 3, four 4: 1 + 10 + 10"

    def test_the_population_floor_changes_the_count_of_work_not_the_count_of_answers(
        self, tmp_path: Path
    ) -> None:
        database = mixed_length_database(tmp_path / "places.sqlite3")
        no_floor = SearchRequest(database=database, n=1)
        high_floor = SearchRequest(database=database, n=1, min_population=40_000)

        with_no_floor = open_search(no_floor)
        with_high_floor = open_search(high_floor)

        assert with_no_floor.stats.places == 9
        assert with_high_floor.stats.places == 6
        assert with_high_floor.stats.candidate_pairs < with_no_floor.stats.candidate_pairs

    def test_a_search_stopped_halfway_reports_a_fraction_of_its_candidates(
        self, tmp_path: Path
    ) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=400)
        session = open_search(
            SearchRequest(database=database, n=NOISY_CHARACTER_DIFFERENCE),
            control=SearchControl(poll_every_comparisons=512),
        )
        assert next(session.pairs, None) is not None, "the fixture plants a pair at the head"
        session.control.cancel("the reader stopped reading")
        session.close()

        assert 0 < session.stats.pairs_compared < session.stats.candidate_pairs
        assert session.stats.share_compared() is not None
        assert session.stats.share_compared() < 1.0

    def test_a_search_with_nothing_to_compare_says_so_rather_than_dividing_by_zero(
        self, tmp_path: Path
    ) -> None:
        rows = [dump_row(31, "solo", 1.0, 2.0, population=5)]
        database = place_database(tmp_path / "places.sqlite3", rows)
        session = open_search(
            SearchRequest(database=database, n=1, min_population=1_000)
        )
        list(session.pairs)

        assert session.stats.candidate_pairs == 0
        assert session.stats.share_compared() is None


class TestTheCostEstimate:
    def test_no_time_is_claimed_before_anything_has_been_measured(self) -> None:
        estimate = MeasuredRate().estimate(10_000_000)

        assert estimate.candidate_pairs == 10_000_000, "the pair count is exact either way"
        assert estimate.seconds is None
        assert "not measured" in estimate.basis

    def test_a_search_that_has_run_makes_an_estimate_of_the_next_one(self) -> None:
        rate = MeasuredRate()
        rate.record(comparisons=1_000, seconds=0.5)

        estimate = rate.estimate(10_000)

        assert estimate.comparisons_per_second == pytest.approx(2_000.0)
        assert estimate.seconds == pytest.approx(5.0)
        assert "measured" in estimate.basis and "not measured" not in estimate.basis

    def test_nothing_measured_is_nothing_recorded(self) -> None:
        rate = MeasuredRate()
        for comparisons, seconds in ((0, 1.0), (1_000, 0.0), (-5, 1.0)):
            rate.record(comparisons=comparisons, seconds=seconds)

        assert rate.comparisons_per_second is None


class TestTheBestSoFar:
    def test_the_default_best_list_is_the_furthest_fifty(self, tmp_path: Path) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1))
        streamed = [matched.distance_km for matched in session.pairs]

        assert session.limit == DEFAULT_TOP_K
        assert session.direction is Ranking.GREATEST
        assert [ranked.distance_km for ranked in session.snapshot()] == sorted(
            streamed, reverse=True
        ), "fifty is more than this fixture holds, so the whole stream is in the list"

    def test_the_snapshot_sequence_is_the_one_the_core_ranking_produces(
        self, tmp_path: Path
    ) -> None:
        """A client sees exactly the improving lists the core would produce from those pairs.

        Nothing about the snapshot sequence depends on timing, so the browser and the core
        cannot drift apart without this test noticing.
        """
        database = two_pair_database(tmp_path / "places.sqlite3")
        request = SearchRequest(database=database, n=1, top_k=2)
        session = open_search(request)

        seen: list[tuple[float, ...]] = []
        improvements = 0
        for _ in session.pairs:
            if session.stats.improvements != improvements:
                improvements = session.stats.improvements
                seen.append(tuple(ranked.distance_km for ranked in session.snapshot()))

        reference = open_search(request)
        expected = [
            tuple(ranked.distance_km for ranked in snapshot)
            for snapshot in snapshots(reference.pairs, limit=2)
        ]
        assert seen == expected
        assert len(seen) == session.stats.improvements

    def test_a_least_first_request_watches_the_other_end_of_the_range(
        self, tmp_path: Path
    ) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(
            SearchRequest(database=database, n=1, top_k=1, direction=Ranking.LEAST),
                    )
        list(session.pairs)

        [ranked] = session.snapshot()
        assert ranked.distance_km == min(
            pair[2] for pair in pairs_of(SearchRequest(database=database, n=1))
        )

    def test_a_ranked_pair_carries_what_a_result_row_needs(self, tmp_path: Path) -> None:
        database = two_pair_database(tmp_path / "places.sqlite3")
        session = open_search(SearchRequest(database=database, n=1, top_k=1))
        list(session.pairs)

        [ranked] = session.snapshot()
        assert (ranked.first.asciiname, ranked.second.asciiname) == ("aloha", "alpha"), (
            "the fixture's furthest pair, and the database's candidate order: population "
            "ascending, so the smaller city is `first`"
        )
        assert ranked.first.country_code and ranked.second.country_code
        assert ranked.first.population and ranked.second.population
        assert ranked.character_difference == 1
        assert ranked.differing_position_indexes == differing_position_indexes("alpha", "aloha")
        assert ranked.differing_position_indexes == (2,)

    @pytest.mark.parametrize(
        "bad", [{"direction": "nearest"}, {"direction": ""}, {"top_k": 0}, {"top_k": "many"}]
    )
    def test_a_bad_ranking_parameter_is_refused_like_any_other_bad_parameter(
        self, tmp_path: Path, bad: dict[str, object]
    ) -> None:
        """A refused request is an exception a caller can report, never a stream of nothing."""
        database = two_pair_database(tmp_path / "places.sqlite3")
        with pytest.raises(InvalidSearchRequest):
            open_search(SearchRequest(database=database, n=1, **bad))  # type: ignore[arg-type]
