"""Sweeping a range of character differences from one place.

The rules this pins belong to from-a-place mode (#9) and to nothing else: the range starts at
zero, every value in it is reported whether or not it found anything, identical names can be
left out of the *answer* without being left out of the *count*, and a place is never measured
against itself.

Where `core.scan.matching_pairs` compares one place against its partners at exactly one
threshold, `core.sweep.Sweep` compares it against the same shape of iterable at a whole range and
keeps a tally per value. The two share `MatchedPair`, the distance function and the cooperative
`cancel`/`progress` checks; what they do not share is what a zero means, which is why the sweep is
its own module rather than a flag on the scan.
"""

from __future__ import annotations

import pytest

from distnsimilarity.core.places import Place
from distnsimilarity.core.scan import MatchedPair
from distnsimilarity.core.sweep import (
    DEFAULT_DIFFERENCE_LIMIT,
    DifferenceCounter,
    DifferenceRange,
    Sweep,
)


def place(
    geonameid: int, asciiname: str, latitude: float, longitude: float, *, population: int = 1_000
) -> Place:
    return Place(
        geonameid=geonameid,
        asciiname=asciiname,
        display_name=asciiname,
        latitude=latitude,
        longitude=longitude,
        population=population,
        country_code="XX",
    )


#: `Lima` and the partners a sweep over four-letter names would be handed: the same name in
#: another country (zero differing characters), then one position changed, then two, then three,
#: then a name one position longer than any of them.
LIMA = place(1, "Lima", -12.0464, -77.0428, population=9_750_000)
LIMA_US = place(2, "Lima", 40.5736, -84.1084, population=42_000)
LINA = place(3, "Lina", 10.0, 10.0, population=30_000)
LINO = place(4, "Lino", 20.0, 20.0, population=20_000)
BOBA = place(5, "Boba", 30.0, 30.0, population=10_000)
LIMAS = place(6, "Limas", 40.0, 40.0, population=5_000)


def names(matched: list[MatchedPair]) -> list[str]:
    return [pair.second.asciiname for pair in matched]


class TestTheRange:
    def test_it_starts_at_zero_and_sweeps_up_to_its_limit(self) -> None:
        assert DifferenceRange(limit=3).values == (0, 1, 2, 3)

    def test_a_range_of_zero_is_a_range_of_one_value_and_that_value_is_zero(self) -> None:
        assert DifferenceRange(limit=0).values == (0,)

    def test_the_default_reaches_far_enough_to_reach_a_confusable_name(self) -> None:
        assert DifferenceRange().limit == DEFAULT_DIFFERENCE_LIMIT

    def test_a_range_cannot_reach_below_zero(self) -> None:
        with pytest.raises(ValueError, match="below zero"):
            DifferenceRange(limit=-1)

    def test_the_top_of_a_range_is_a_whole_number_of_characters(self) -> None:
        for wrong in ("3", 1.5, True, None):
            with pytest.raises(TypeError):
                DifferenceRange(limit=wrong)

    def test_excluding_identical_names_is_a_yes_or_a_no(self) -> None:
        with pytest.raises(TypeError, match="yes or no"):
            DifferenceRange(limit=3, exclude_identical_names="maybe")

    def test_zero_is_within_every_range_because_zero_is_where_duplicate_names_live(self) -> None:
        assert DifferenceRange(limit=0).within(0) is True

    def test_a_value_above_the_limit_is_not_part_of_the_range(self) -> None:
        difference = DifferenceRange(limit=2)
        assert difference.within(2) is True
        assert difference.within(3) is False


class TestWhatEachValueProduced:
    """AC: "the number of candidates produced by each character-difference value is shown"."""

    def counts(self, difference: DifferenceRange, partners: list[Place]) -> dict[int, int]:
        sweep = Sweep(LIMA, difference)
        list(sweep.pairs(partners))
        return {count.character_difference: count.pairs for count in sweep.counts}

    def test_every_value_in_the_range_is_reported_including_the_ones_that_found_nothing(self) -> None:
        sweep = Sweep(LIMA, DifferenceRange(limit=4))
        list(sweep.pairs([LIMA_US, LINA]))
        assert [count.character_difference for count in sweep.counts] == [0, 1, 2, 3, 4]
        assert [count.pairs for count in sweep.counts] == [1, 1, 0, 0, 0], (
            "a value that found nothing is reported as having found nothing: a tally that only "
            "mentioned the values with pairs would make a wide range look like a narrow one"
        )

    def test_each_value_counts_the_partners_at_that_many_differing_characters(self) -> None:
        # `Lina` differs in one position, `Lino` in two, `Boba` in three.
        assert self.counts(DifferenceRange(limit=3), [LIMA_US, LINA, LINO, BOBA]) == {
            0: 1,
            1: 1,
            2: 1,
            3: 1,
        }

    def test_a_partner_beyond_the_limit_is_not_counted_at_any_value(self) -> None:
        assert self.counts(DifferenceRange(limit=1), [LIMA_US, LINA, LINO, BOBA]) == {0: 1, 1: 1}

    def test_the_counts_come_from_the_same_walk_as_the_pairs(self) -> None:
        """One sweep, one walk: the tally and the list are two views of the same comparisons."""
        sweep = Sweep(LIMA, DifferenceRange(limit=2))
        matched = list(sweep.pairs([LIMA_US, LINA, LINO, BOBA]))
        assert names(matched) == ["Lima", "Lina", "Lino"]
        assert [count.pairs for count in sweep.counts] == [1, 1, 1]

    def test_an_excluded_value_is_counted_and_marked_rather_than_left_out(self) -> None:
        sweep = Sweep(LIMA, DifferenceRange(limit=1, exclude_identical_names=True))
        list(sweep.pairs([LIMA_US, LINA]))
        [zero, one] = sweep.counts
        assert (zero.character_difference, zero.pairs, zero.excluded) == (0, 1, True)
        assert (one.character_difference, one.pairs, one.excluded) == (1, 1, False)

    def test_a_value_is_only_marked_excluded_when_the_reader_asked_for_it(self) -> None:
        sweep = Sweep(LIMA, DifferenceRange(limit=1))
        list(sweep.pairs([LIMA_US]))
        assert [count.excluded for count in sweep.counts] == [False, False]

    def test_the_tally_says_how_many_pairs_it_holds_including_the_excluded_ones(self) -> None:
        counter = DifferenceCounter(DifferenceRange(limit=1, exclude_identical_names=True))
        counter.record(0)
        counter.record(1)
        assert counter.pairs == 2

    def test_counting_a_value_outside_the_range_is_a_bug_and_says_which_value(self) -> None:
        counter = DifferenceCounter(DifferenceRange(limit=1))
        with pytest.raises(ValueError, match="outside the range"):
            counter.record(2)


class TestThePairsASweepHandsOut:
    def test_a_sweep_reports_every_value_in_the_range_at_once(self) -> None:
        """The one place a range beats a threshold: three separate searches become one walk."""
        matched = list(Sweep(LIMA, DifferenceRange(limit=2)).pairs([LIMA_US, LINA, LINO, BOBA]))
        assert names(matched) == ["Lima", "Lina", "Lino"]

    def test_a_partner_further_apart_in_the_names_than_the_limit_never_appears(self) -> None:
        matched = list(Sweep(LIMA, DifferenceRange(limit=0)).pairs([LIMA_US, LINA, LINO, BOBA]))
        assert names(matched) == ["Lima"]

    def test_a_name_shared_across_countries_is_a_result_at_zero_differing_characters(self) -> None:
        """AC: "a name shared across countries produces results at differing-character count 0"."""
        [pair] = list(Sweep(LIMA, DifferenceRange(limit=3)).pairs([LIMA_US]))
        assert (pair.first.asciiname, pair.second.asciiname) == ("Lima", "Lima")
        assert pair.distance_km == pytest.approx(5896.192, abs=0.001), (
            "the distance is between the two places that share the name, which is the whole "
            "point of asking about zero: these are two different Limas, not one place"
        )

    def test_identical_names_can_be_left_out_of_the_answer(self) -> None:
        difference = DifferenceRange(limit=1, exclude_identical_names=True)
        assert names(list(Sweep(LIMA, difference).pairs([LIMA_US, LINA]))) == ["Lina"]

    def test_a_place_is_not_measured_against_itself(self) -> None:
        """The origin is in the name-length group it is being walked against, and is not a find."""
        matched = list(Sweep(LIMA, DifferenceRange(limit=0)).pairs([LIMA, LIMA_US]))
        assert names(matched) == ["Lima"]
        assert [pair.second.geonameid for pair in matched] == [LIMA_US.geonameid]

    def test_a_distance_is_measured_from_the_selected_place_not_from_another_with_the_same_name(
        self,
    ) -> None:
        """AC: "every result's distance is measured from the selected place's coordinates".

        Both these searches find the same name, `Lina`, and both are asked for by somebody who
        typed four letters. The only difference is which place was picked out of the list of
        matches, and the kilometres have to move when that choice moves.
        """
        [from_peru] = list(Sweep(LIMA, DifferenceRange(limit=1)).pairs([LINA]))
        [from_ohio] = list(Sweep(LIMA_US, DifferenceRange(limit=1)).pairs([LINA]))
        assert from_peru.distance_km == pytest.approx(9921.888, abs=0.001)
        assert from_ohio.distance_km != from_peru.distance_km
        [back_again] = list(Sweep(LINA, DifferenceRange(limit=1)).pairs([LIMA]))
        assert back_again.distance_km == pytest.approx(from_peru.distance_km), (
            "the same two places are the same distance apart whichever end the search starts at"
        )

    def test_a_partner_of_a_different_name_length_is_refused_naming_both_names(self) -> None:
        """Equal lengths are what a candidate walk guarantees, so this is a broken candidate set."""
        with pytest.raises(ValueError, match="different length"):
            list(Sweep(LIMA, DifferenceRange(limit=1)).pairs([LIMAS]))


class TestBeingToldToStop:
    """A sweep is a stream, so the same cooperative rules as a scan apply to it."""

    def test_a_cancelled_sweep_keeps_the_pairs_it_found_and_the_counts_it_made(self) -> None:
        partners = [LIMA_US, LINA, LINO, BOBA]
        asks = {"times": 0}

        def already_stopped() -> bool:
            asks["times"] += 1
            return asks["times"] >= 2

        sweep = Sweep(LIMA, DifferenceRange(limit=3))
        matched = list(sweep.pairs(partners, cancel=already_stopped, poll_every_comparisons=1))
        assert names(matched) == ["Lima"], (
            "the walk ends between two comparisons, keeping what it yielded"
        )
        assert sum(count.pairs for count in sweep.counts) == 1

    def test_a_sweep_counts_the_partners_it_compared(self) -> None:
        counted: list[int] = []
        list(
            Sweep(LIMA, DifferenceRange(limit=3)).pairs(
                [LIMA_US, LINA, LINO, BOBA],
                progress=counted.append,
                poll_every_comparisons=2,
            )
        )
        assert sum(counted) == 4, "every partner handed over was a comparison, found or not"

    def test_the_comparisons_are_counted_even_when_the_walk_stops_early(self) -> None:
        counted: list[int] = []
        list(
            Sweep(LIMA, DifferenceRange(limit=3)).pairs(
                [LIMA_US, LINA, LINO, BOBA],
                cancel=lambda: True,
                progress=counted.append,
                poll_every_comparisons=1,
            )
        )
        assert sum(counted) == 0, "a sweep stopped before its first comparison compared nothing"


class TestASweepIsBuiltFromAPlaceAndARange:
    def test_it_starts_from_a_place_and_nothing_else(self) -> None:
        with pytest.raises(TypeError, match="starts from a place"):
            Sweep("Lima", DifferenceRange(limit=1))  # type: ignore[arg-type]

    def test_it_walks_a_range_and_not_a_single_threshold(self) -> None:
        with pytest.raises(TypeError, match="character-difference range"):
            Sweep(LIMA, 1)  # type: ignore[arg-type]

    def test_a_sweep_hands_over_what_it_found_as_plain_data(self) -> None:
        """The interface an adapter builds a response out of, with no adapter in the room."""
        sweep = Sweep(LIMA, DifferenceRange(limit=1, exclude_identical_names=True))
        list(sweep.pairs([LIMA_US, LINA]))
        assert sweep.difference.as_dict() == {
            "limit": 1,
            "exclude_identical_names": True,
        }
        assert [count.as_dict() for count in sweep.counts] == [
            {"character_difference": 0, "pairs": 1, "excluded": True},
            {"character_difference": 1, "pairs": 1, "excluded": False},
        ]
