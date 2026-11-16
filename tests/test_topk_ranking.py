"""The running top-K: what "the best so far" means, and when it counts as having improved.

A scan streams pairs while a client watches a list get better as it goes. Two questions have
to be answered exactly, or a browser and the core will disagree about what the list holds:

* **Which pairs are the best** - greatest distance first, or least, with a tie-break that
  does not depend on the order the pairs happened to arrive in.
* **When the list has improved** - a snapshot is only worth sending when the ranked set
  actually changed, which is what lets the transport coalesce improvements rather than
  flood a client with one message per pair.

Every distance here is chosen by the test rather than computed, because ranking never
recomputes one. The character difference *is* computed, from the names, and pinned as such.
"""

from __future__ import annotations

import random
from typing import Iterable

import pytest

from distnsimilarity.core.matching import differing_position_indexes
from distnsimilarity.core.places import Place
from distnsimilarity.core.scan import MatchedPair
from distnsimilarity.core.topk import (
    DEFAULT_TOP_K,
    Ranking,
    RankedPair,
    TopK,
    parse_direction,
    snapshots,
)


def place(
    geonameid: int,
    asciiname: str,
    latitude: float,
    longitude: float,
    *,
    population: int = 1_000,
    country_code: str = "XX",
    display_name: str | None = None,
) -> Place:
    """One place, as the core sees it, with no dump in the way."""
    return Place(
        geonameid=geonameid,
        asciiname=asciiname,
        display_name=asciiname if display_name is None else display_name,
        latitude=float(latitude),
        longitude=float(longitude),
        population=int(population),
        country_code=country_code,
    )


def pair(first: Place, second: Place, distance_km: float) -> MatchedPair:
    """A reported pair with a distance chosen by the test.

    Ranking never recomputes a distance, so a test can hand it exactly the numbers it wants
    ordered, and still read `differing_positions` off the names to check it agrees with them.
    """
    return MatchedPair(first=first, second=second, distance_km=distance_km)




def a_pair(geonameid: int, distance: float) -> MatchedPair:
    """Two names one character apart, so the character difference is always 1."""
    return pair(
        place(geonameid, "Ab", 0.0, 0.0), place(geonameid + 100, "Ae", 0.0, 1.0), distance
    )


def snapshots_of(
    stream: Iterable[MatchedPair], limit: int, direction: Ranking = Ranking.GREATEST
) -> list[tuple[RankedPair, ...]]:
    """The whole snapshot sequence at once. It is a stream in production; a test wants it whole."""
    return list(snapshots(stream, limit=limit, direction=direction))


def distances(snapshot: tuple[RankedPair, ...]) -> list[float]:
    return [ranked.distance_km for ranked in snapshot]


def ids(snapshot: tuple[RankedPair, ...]) -> list[tuple[int, int]]:
    return [(ranked.first.geonameid, ranked.second.geonameid) for ranked in snapshot]


class TestGreatestFirst:
    def test_the_best_is_the_biggest_distance(self) -> None:
        top = TopK(3)

        assert top.add(a_pair(1, 100.0)) is True
        assert top.add(a_pair(2, 900.0)) is True
        assert distances(top.snapshot()) == [900.0, 100.0]

    def test_a_full_top_k_keeps_the_biggest_and_drops_the_smallest(self) -> None:
        top = TopK(2)
        for distance in (100.0, 200.0, 50.0, 300.0):
            top.add(a_pair(1, distance))

        assert distances(top.snapshot()) == [300.0, 200.0]
        assert top.full is True

    def test_a_pair_too_small_to_enter_a_full_list_changes_nothing(self) -> None:
        top = TopK(2)
        top.add(a_pair(1, 100.0))
        top.add(a_pair(2, 200.0))

        assert top.add(a_pair(3, 90.0)) is False
        assert distances(top.snapshot()) == [200.0, 100.0]

    def test_beating_the_worst_entry_is_an_improvement(self) -> None:
        top = TopK(2)
        top.add(a_pair(1, 10.0))
        top.add(a_pair(2, 20.0))

        assert top.add(a_pair(3, 15.0)) is True
        assert distances(top.snapshot()) == [20.0, 15.0]

    def test_matching_the_worst_entry_of_a_full_list_is_not_an_improvement(self) -> None:
        """The list holds the same two distances either way, so there is nothing new to send."""
        top = TopK(2)
        top.add(a_pair(1, 10.0))
        top.add(a_pair(2, 20.0))

        assert top.add(a_pair(3, 10.0)) is False

    def test_a_self_pair_is_ranked_like_any_other_pair(self) -> None:
        """A place is a pair with itself, so `least` puts it first at zero distance."""
        self_pair = pair(place(7, "Lima", 0.0, 0.0), place(7, "Lima", 0.0, 0.0), 0.0)
        top = TopK(2, Ranking.LEAST)

        top.add(a_pair(1, 500.0))
        assert top.add(self_pair) is True
        assert distances(top.snapshot()) == [0.0, 500.0]


class TestLeastFirst:
    def test_the_best_is_the_smallest_distance(self) -> None:
        top = TopK(3, Ranking.LEAST)

        top.add(a_pair(1, 100.0))
        top.add(a_pair(2, 900.0))

        assert distances(top.snapshot()) == [100.0, 900.0]

    def test_a_full_top_k_keeps_the_smallest_and_drops_the_biggest(self) -> None:
        top = TopK(2, Ranking.LEAST)
        for distance in (100.0, 200.0, 5_000.0, 50.0):
            top.add(a_pair(1, distance))

        assert distances(top.snapshot()) == [50.0, 100.0]

    def test_a_pair_too_big_to_enter_a_full_list_changes_nothing(self) -> None:
        top = TopK(2, Ranking.LEAST)
        top.add(a_pair(1, 100.0))
        top.add(a_pair(2, 200.0))

        assert top.add(a_pair(3, 900.0)) is False
        assert distances(top.snapshot()) == [100.0, 200.0]

    def test_the_two_directions_keep_different_ends_of_the_same_stream(self) -> None:
        stream = [a_pair(index, float(index * 100)) for index in range(1, 6)]

        greatest = TopK(2)
        least = TopK(2, Ranking.LEAST)
        for matched in stream:
            greatest.add(matched)
            least.add(matched)

        assert distances(greatest.snapshot()) == [500.0, 400.0]
        assert distances(least.snapshot()) == [100.0, 200.0]


class TestTies:
    def test_equal_distances_are_ordered_by_geonames_id_not_by_arrival(self) -> None:
        high_ids = a_pair(10, 100.0)
        low_ids = a_pair(2, 100.0)

        assert ids(snapshots_of([high_ids, low_ids], limit=2)[-1]) == [(2, 102), (10, 110)]
        assert ids(snapshots_of([low_ids, high_ids], limit=2)[-1]) == [(2, 102), (10, 110)], (
            "the same final list whichever arrived first"
        )

    def test_a_tied_pair_cannot_bump_the_pair_it_ties_with(self) -> None:
        """Otherwise a long scan would keep resending the same list for every equal distance."""
        incumbent = a_pair(2, 100.0)
        challenger = a_pair(90, 100.0)

        assert snapshots_of([incumbent, challenger], limit=1) == snapshots_of(
            [incumbent], limit=1
        ), "one snapshot: the challenger ties and loses to the incumbent"

    def test_an_equal_replacement_is_not_an_improvement_in_either_direction(self) -> None:
        for direction in Ranking:
            top = TopK(1, direction)
            top.add(a_pair(2, 100.0))

            assert top.add(a_pair(3, 100.0)) is False, direction


class TestSnapshotSequence:
    def test_the_first_snapshot_arrives_with_the_first_pair(self) -> None:
        stream = snapshots_of([a_pair(1, 10.0), a_pair(2, 20.0)], limit=1)

        assert [distances(snapshot) for snapshot in stream] == [[10.0], [20.0]]

    def test_a_pair_that_changes_nothing_produces_no_snapshot(self) -> None:
        stream = snapshots_of([a_pair(1, 30.0), a_pair(2, 20.0), a_pair(3, 10.0)], limit=2)

        assert [distances(snapshot) for snapshot in stream] == [[30.0], [30.0, 20.0]]

    def test_no_pairs_no_snapshots(self) -> None:
        assert snapshots_of([], limit=5) == []

    def test_every_snapshot_differs_from_the_one_before_it(self) -> None:
        stream = [a_pair(index, float((index * 37) % 500)) for index in range(1, 40)]

        seen = snapshots_of(stream, limit=4)
        assert len(seen) > 3, "the fixture has to actually improve a few times"
        for previous, following in zip(seen, seen[1:]):
            assert len(following) >= len(previous)
            assert distances(following) != distances(previous), (
                "a snapshot identical to the last one should never have been emitted"
            )

    @pytest.mark.parametrize("limit", [1, 2, 3, 7, DEFAULT_TOP_K])
    @pytest.mark.parametrize("direction", list(Ranking))
    def test_the_final_snapshot_is_the_whole_stream_sorted(
        self, limit: int, direction: Ranking
    ) -> None:
        rng = random.Random(20241115)
        stream = [a_pair(index, round(rng.uniform(0.0, 20_000.0), 6)) for index in range(1, 41)]

        final = snapshots_of(stream, limit=limit, direction=direction)[-1]

        expected = sorted(
            stream,
            key=lambda matched: matched.distance_km,
            reverse=direction is Ranking.GREATEST,
        )[:limit]
        assert distances(final) == [matched.distance_km for matched in expected]

    def test_the_character_difference_is_counted_from_the_names(self) -> None:
        """The payload tells a person how alike the names are, so it is derived here and not
        handed in by whoever is scanning: a reported pair's difference is a property of its
        two names. The count is what a list row shows; the positions are what highlights them.
        """
        one = pair(place(1, "Aba", 0.0, 0.0), place(2, "Abe", 0.0, 1.0), 111.0)
        two = pair(place(3, "Aba", 0.0, 0.0), place(4, "Ace", 0.0, 2.0), 222.0)

        ranked = snapshots_of([two, one], limit=2)[-1]

        assert [ranked[0].character_difference, ranked[1].character_difference] == [2, 1]
        assert ranked[0].differing_position_indexes == differing_position_indexes("Aba", "Ace")

    def test_a_snapshot_is_immutable_once_emitted(self) -> None:
        top = TopK(2)
        top.add(a_pair(1, 10.0))
        first = top.snapshot()

        top.add(a_pair(2, 20.0))

        assert distances(first) == [10.0], "a client may already be holding this one"
        assert distances(top.snapshot()) == [20.0, 10.0]

    def test_an_empty_ranking_snapshots_as_nothing(self) -> None:
        assert TopK(3).snapshot() == ()


class TestWhatIsRefused:
    @pytest.mark.parametrize("limit", [0, -1, -50])
    def test_a_top_k_below_one_is_refused(self, limit: int) -> None:
        with pytest.raises(ValueError):
            TopK(limit)

    @pytest.mark.parametrize("limit", [1.5, "10", None, True])
    def test_a_top_k_that_is_not_a_count_is_refused(self, limit: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            TopK(limit)  # type: ignore[arg-type]

    def test_an_unknown_direction_names_the_ones_that_exist(self) -> None:
        with pytest.raises(ValueError) as raised:
            parse_direction("nearest")

        message = str(raised.value)
        assert "nearest" in message
        assert "greatest" in message and "least" in message

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("greatest", Ranking.GREATEST),
            ("least", Ranking.LEAST),
            ("  Least  ", Ranking.LEAST),
            (Ranking.LEAST, Ranking.LEAST),
        ],
    )
    def test_a_direction_can_be_spelled_in_several_ways(
        self, given: object, expected: Ranking
    ) -> None:
        assert parse_direction(given) is expected  # type: ignore[arg-type]

    def test_the_default_is_fifty_greatest(self) -> None:
        assert DEFAULT_TOP_K == 50
        assert TopK(DEFAULT_TOP_K).direction is Ranking.GREATEST

    def test_snapshots_refuse_the_same_bad_arguments_as_topk(self) -> None:
        with pytest.raises(ValueError):
            list(snapshots([a_pair(1, 1.0)], limit=0))
        with pytest.raises(ValueError):
            list(snapshots([a_pair(1, 1.0)], limit=2, direction="sideways"))  # type: ignore[arg-type]
