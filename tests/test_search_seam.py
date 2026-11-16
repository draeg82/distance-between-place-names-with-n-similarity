"""The search seam: one way to open a search, one way to stop a running one.

Issue #6 put both adapters - the command line and the HTTP API - behind `open_search`, so
this is where the things they share are pinned: which pairs come out, in which order, what
a bad request costs, and what a cancelled search leaves behind. The adapter tests only check
that their adapter passes this through.

The cancellation tests need a scan long enough to interrupt, which is what `noisy_dump` is
for: a planted pair first, then a long stretch of equal-length names that match nothing.
Everything else runs on hand-checkable fixtures.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from distnsimilarity.core.matching import differing_position_indexes
from distnsimilarity.core.places import read_places
from distnsimilarity.core.scan import scan
from distnsimilarity.core.topk import DEFAULT_TOP_K, Ranking, snapshots
from distnsimilarity.datasets import DatasetNotAvailableError
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
    import_dump,
    noisy_dump,
    write_dump,
)


def two_pair_dump(path: Path) -> Path:
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
    return write_dump(path, rows)


def pairs_of(request: SearchRequest, data_dir: Path | None = None) -> list[tuple[str, str, float]]:
    """Every pair a search reports, as `(name1, name2, distance)` - the shape tests compare."""
    session = open_search(request, data_dir=data_dir)
    return [(m.first.asciiname, m.second.asciiname, m.distance_km) for m in session.pairs]


class TestOpeningASearch:
    def test_a_search_names_its_dataset_and_how_many_places_it_holds(
        self, tmp_path: Path
    ) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1), data_dir=tmp_path)

        assert session.name == "cities6.txt"
        assert session.stats.places == 6, (
            f"the search reported {session.stats.places} places for a 6-row dump"
        )
        assert session.source == "dump"

    def test_the_pairs_come_in_the_order_the_core_scan_reports_them(
        self, tmp_path: Path
    ) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        request = SearchRequest(dataset=str(dump), n=1)

        through_the_seam = pairs_of(request, tmp_path)
        places = read_places(dump)
        straight_off_the_scan = [
            (m.first.asciiname, m.second.asciiname, m.distance_km) for m in scan(places, 1)
        ]

        assert through_the_seam == straight_off_the_scan, (
            "the seam and the scan disagree about which pairs or what order - through the "
            f"seam {through_the_seam}, straight off the scan {straight_off_the_scan}"
        )

    def test_a_dataset_that_is_not_there_is_refused_before_any_pair(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetNotAvailableError) as refused:
            open_search(SearchRequest(dataset="cities99999.txt", n=1), data_dir=tmp_path)
        assert "cities99999.txt" in str(refused.value)

    def test_a_negative_character_difference_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidSearchRequest) as refused:
            SearchRequest(dataset=str(two_pair_dump(tmp_path / "cities6.txt")), n=-1)
        assert "cannot be negative" in str(refused.value)

    def test_a_request_naming_no_dataset_is_refused(self) -> None:
        with pytest.raises(InvalidSearchRequest) as refused:
            SearchRequest(dataset="   ", n=1)
        assert "name a dataset" in str(refused.value)

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
        dump = write_dump(tmp_path / "cities6.txt", rows)
        request = SearchRequest(dataset=str(dump), n=1)

        without_a_floor = pairs_of(request, tmp_path)
        with_a_floor = pairs_of(SearchRequest(dataset=str(dump), n=1, min_population=1_000), tmp_path)

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
        dump = two_pair_dump(tmp_path / "cities6.txt")
        with_prefilter = pairs_of(SearchRequest(dataset=str(dump), n=1), tmp_path)
        without_prefilter = pairs_of(
            SearchRequest(dataset=str(dump), n=1, equal_length_prefilter=False), tmp_path
        )
        assert with_prefilter == without_prefilter

    def test_the_same_request_can_be_searched_from_the_imported_cache(self, tmp_path: Path) -> None:
        """One request, two sources.

        The cache walks its candidates by population, so a pair can come out with its places
        the other way round from the dump's file order. Which place is `first` is the
        candidate order, not a different pair: the set of pairs and the distances match.
        """
        dump = write_dump(
            tmp_path / "cities7.txt",
            [
                dump_row(21, "alpha", 40.0, -100.0, population=90_000),
                dump_row(22, "aleha", 41.0, -101.0, population=80_000),
                dump_row(23, "brant", 43.0, -103.0, population=60_000),
            ],
        )
        database = import_dump(tmp_path / "places.sqlite3", dump)
        database.close()

        from_the_dump = pairs_of(SearchRequest(dataset=str(dump), n=1), tmp_path)
        session = open_search(
            SearchRequest(dataset=str(dump), n=1, database=tmp_path / "places.sqlite3"),
            data_dir=tmp_path,
        )
        from_the_cache = [
            (m.first.asciiname, m.second.asciiname, m.distance_km) for m in session.pairs
        ]

        assert session.source == "database"
        from_cache = sorted(
            (tuple(sorted((first, second))), round(distance, 9))
            for first, second, distance in from_the_cache
        )
        from_dump = sorted(
            (tuple(sorted((first, second))), round(distance, 9))
            for first, second, distance in from_the_dump
        )
        assert from_cache == from_dump, (
            "the cache and the dump disagree about which pairs match, or how far apart they "
            f"are: {from_cache} against {from_dump}"
        )
        assert session.stats.places == 3

    def test_a_cache_with_no_places_in_it_is_refused_by_name(self, tmp_path: Path) -> None:
        empty = tmp_path / "places.sqlite3"
        sqlite3.connect(str(empty)).close()

        with pytest.raises(CacheNotImportedError) as refused:
            open_search(
                SearchRequest(dataset="anything", n=1, database=empty), data_dir=tmp_path
            )
        assert "distnsimilarity-import" in str(refused.value), (
            "the refusal has to say what to do about it"
        )


class TestStoppingASearch:
    def test_a_search_that_is_left_alone_reports_everything_it_found(self, tmp_path: Path) -> None:
        dump = noisy_dump(tmp_path / "cities8.txt", places=300)
        session = open_search(SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE))
        pairs = list(session.pairs)

        assert session.stats.completed is True
        assert session.stats.cancelled is False
        assert session.stats.pairs_emitted == len(pairs) == 1, (
            f"the noisy fixture was supposed to hold exactly the planted pair, found {pairs}"
        )
        assert session.stats.comparison_polls > 0, "the scan polled its cancellation check"

    def test_a_reader_that_stops_gets_exactly_the_pairs_before_that_point(self, tmp_path: Path) -> None:
        dump = noisy_dump(tmp_path / "cities8.txt", places=1_500)
        request = SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE)
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
        dump = noisy_dump(tmp_path / "cities8.txt", places=1_500)
        request = SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE)

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
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1))
        session.control.cancel("nobody wanted the results")

        assert list(session.pairs) == []
        assert session.stats.pairs_emitted == 0
        assert session.stats.completed is False

    def test_cancelling_a_search_that_is_already_over_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1))
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
        dump = noisy_dump(tmp_path / "cities8.txt", places=1_500)
        control = SearchControl(poll_every_comparisons=256)
        session = open_search(
            SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE), control=control
        )
        next(session.pairs)
        control.cancel("stopped from outside")

        assert list(session.pairs) == []
        assert control.polls > 0

    def test_a_scan_polls_its_check_once_per_poll_interval(self, tmp_path: Path) -> None:
        """The poll interval is the difference between stopping in a millisecond and stopping
        after the next few million comparisons, so its arithmetic is pinned here."""
        dump = noisy_dump(tmp_path / "cities8.txt", places=400)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE), control=control
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
        dump = noisy_dump(tmp_path / "cities8.txt", places=600)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE), control=control
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
        dump = noisy_dump(tmp_path / "cities8.txt", places=600)
        control = SearchControl(poll_every_comparisons=64)
        session = open_search(
            SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE), control=control
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


def mixed_length_dump(path: Path) -> Path:
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
    return write_dump(path, rows)


class TestWhatTheSearchIsAboutToDo:
    """The cost of a search, said before it runs, in the same units as the work it does."""

    def test_a_search_counts_its_candidate_pairs_before_comparing_any_of_them(
        self, tmp_path: Path
    ) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1), data_dir=tmp_path)

        assert session.stats.candidate_pairs == 21, "six places, each against itself and later"
        assert session.stats.pairs_compared == 0, "counting the job is not doing it"

    @pytest.mark.parametrize(
        ("prefilter", "expected"), [(True, 21), (False, 45)], ids=["grouped", "everything"]
    )
    def test_the_counted_candidates_are_exactly_the_pairs_the_scan_compares(
        self, tmp_path: Path, prefilter: bool, expected: int
    ) -> None:
        """The estimate and the work are counted by one rule, or a progress bar lies."""
        dump = mixed_length_dump(tmp_path / "cities9.txt")
        session = open_search(
            SearchRequest(dataset=str(dump), n=1, equal_length_prefilter=prefilter),
            data_dir=tmp_path,
        )
        list(session.pairs)

        assert session.stats.candidate_pairs == expected
        assert session.stats.pairs_compared == expected, "the scan compared what it promised"
        assert session.stats.share_compared() == 1.0

    def test_the_length_groups_reduce_the_work_without_reducing_the_pairs(
        self, tmp_path: Path
    ) -> None:
        dump = mixed_length_dump(tmp_path / "cities9.txt")
        grouped = open_search(SearchRequest(dataset=str(dump), n=1), data_dir=tmp_path)
        everything = open_search(
            SearchRequest(dataset=str(dump), n=1, equal_length_prefilter=False),
            data_dir=tmp_path,
        )

        assert grouped.stats.candidate_pairs < everything.stats.candidate_pairs == 45
        assert grouped.stats.candidate_pairs == 21, "one 2-letter, four 3, four 4: 1 + 10 + 10"

    def test_the_population_floor_changes_the_count_of_work_not_the_count_of_answers(
        self, tmp_path: Path
    ) -> None:
        dump = mixed_length_dump(tmp_path / "cities9.txt")
        no_floor = SearchRequest(dataset=str(dump), n=1)
        high_floor = SearchRequest(dataset=str(dump), n=1, min_population=40_000)

        with_no_floor = open_search(no_floor, data_dir=tmp_path)
        with_high_floor = open_search(high_floor, data_dir=tmp_path)

        assert with_no_floor.stats.places == 9
        assert with_high_floor.stats.places == 6
        assert with_high_floor.stats.candidate_pairs < with_no_floor.stats.candidate_pairs

    def test_a_search_stopped_halfway_reports_a_fraction_of_its_candidates(
        self, tmp_path: Path
    ) -> None:
        dump = noisy_dump(tmp_path / "cities40.txt", places=400)
        session = open_search(
            SearchRequest(dataset=str(dump), n=NOISY_CHARACTER_DIFFERENCE),
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
        dump = write_dump(tmp_path / "cities1.txt", rows)
        session = open_search(
            SearchRequest(dataset=str(dump), n=1, min_population=1_000), data_dir=tmp_path
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
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1), data_dir=tmp_path)
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
        dump = two_pair_dump(tmp_path / "cities6.txt")
        request = SearchRequest(dataset=str(dump), n=1, top_k=2)
        session = open_search(request, data_dir=tmp_path)

        seen: list[tuple[float, ...]] = []
        improvements = 0
        for _ in session.pairs:
            if session.stats.improvements != improvements:
                improvements = session.stats.improvements
                seen.append(tuple(ranked.distance_km for ranked in session.snapshot()))

        reference = open_search(request, data_dir=tmp_path)
        expected = [
            tuple(ranked.distance_km for ranked in snapshot)
            for snapshot in snapshots(reference.pairs, limit=2)
        ]
        assert seen == expected
        assert len(seen) == session.stats.improvements

    def test_a_least_first_request_watches_the_other_end_of_the_range(
        self, tmp_path: Path
    ) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(
            SearchRequest(dataset=str(dump), n=1, top_k=1, direction=Ranking.LEAST),
            data_dir=tmp_path,
        )
        list(session.pairs)

        [ranked] = session.snapshot()
        assert ranked.distance_km == min(
            pair[2] for pair in pairs_of(SearchRequest(dataset=str(dump), n=1), tmp_path)
        )

    def test_a_ranked_pair_carries_what_a_result_row_needs(self, tmp_path: Path) -> None:
        dump = two_pair_dump(tmp_path / "cities6.txt")
        session = open_search(SearchRequest(dataset=str(dump), n=1, top_k=1), data_dir=tmp_path)
        list(session.pairs)

        [ranked] = session.snapshot()
        assert (ranked.first.asciiname, ranked.second.asciiname) == ("alpha", "aloha"), (
            "the fixture's furthest pair"
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
        dump = two_pair_dump(tmp_path / "cities6.txt")
        with pytest.raises(InvalidSearchRequest):
            open_search(SearchRequest(dataset=str(dump), n=1, **bad), data_dir=tmp_path)  # type: ignore[arg-type]
