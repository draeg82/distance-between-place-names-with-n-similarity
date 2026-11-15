"""A broken expectation has to explain itself.

These tests break the pins on purpose. What is being asserted is the failure message: it
names the pair of place names, the character-difference threshold, and the expectation that
was violated - so that a regression reads as a statement about places, not as a traceback.
"""

from __future__ import annotations

import pytest

from distnsimilarity.core.places import Place
from distnsimilarity.core.scan import MatchedPair
from pins import NamePair, Spot, pin_distance, pin_match, pin_pairs


def place(geonameid: int, asciiname: str) -> Place:
    return Place(
        geonameid=geonameid,
        asciiname=asciiname,
        display_name=asciiname,
        latitude=0.0,
        longitude=0.0,
        population=0,
        country_code="XX",
    )


def match(first: Place, second: Place) -> MatchedPair:
    return MatchedPair(first, second, 123.0)


class TestABrokenMatchExpectation:
    def test_it_names_the_pair_the_threshold_and_the_count_that_was_found(self) -> None:
        with pytest.raises(AssertionError) as broken:
            pin_match(NamePair("Lima", "Loma"), at=2, expected=True)
        message = str(broken.value)
        assert '"Lima"/"Loma"' in message, message
        assert "n=2" in message, message
        assert "expected a match" in message, message
        assert "differ in 1 of 4 positions" in message, message
        assert "equals n exactly" in message, message

    def test_it_says_so_when_the_pair_is_not_comparable_at_all(self) -> None:
        with pytest.raises(AssertionError) as broken:
            pin_match(NamePair("Aba", "Abba"), at=1, expected=True)
        message = str(broken.value)
        assert '"Aba"/"Abba"' in message, message
        assert "n=1" in message, message
        assert "not comparable at any threshold" in message, message

    def test_a_wrong_negative_expectation_is_caught_too(self) -> None:
        with pytest.raises(AssertionError) as broken:
            pin_match(NamePair("Aba", "Abe"), at=1, expected=False)
        message = str(broken.value)
        assert "expected no match" in message and "n=1" in message, message


class TestABrokenPairExpectation:
    def test_it_names_the_pair_that_is_missing_and_the_one_that_appeared(self) -> None:
        reported = [match(place(1, "Aba"), place(2, "Abe")), match(place(3, "Xyz"), place(4, "Xyw"))]
        with pytest.raises(AssertionError) as broken:
            pin_pairs(reported, at=1, expected=[("Aba", "Abe"), ("Lima", "Loma")])
        message = str(broken.value)
        assert "scan at n=1" in message, message
        assert 'missing "Lima"/"Loma"' in message, message
        assert 'unexpected "Xyz"/"Xyw"' in message, message

    def test_it_counts_what_was_expected_against_what_was_reported(self) -> None:
        with pytest.raises(AssertionError) as broken:
            pin_pairs([], at=3, expected=[("Aba", "Zed")])
        assert "Expected 1 pair(s), got 0." in str(broken.value)


class TestABrokenDistanceExpectation:
    def test_it_names_both_places_the_figure_and_the_tolerance(self) -> None:
        with pytest.raises(AssertionError) as broken:
            pin_distance(
                first=Spot("Kyiv", 50.45, 30.52),
                second=Spot("Cape Town", -33.92, 18.42),
                expected_km=9000.0,
                tolerance_km=0.001,
            )
        message = str(broken.value)
        assert "Kyiv (50.45, 30.52) -> Cape Town (-33.92, 18.42)" in message, message
        assert "expected 9000.0 km" in message, message
        assert "+/- 0.001 km" in message, message
        assert "9456.639" in message, message
