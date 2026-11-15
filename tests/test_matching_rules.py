"""The matching rules, frozen.

Every expected answer here is derivable by hand: the fixtures are two to nine characters
long, and the differing positions can be counted by eye. The core is called directly - no
server, no browser, no command line.

The names are engineered rather than real, except where a real spelling makes the point
about case and accents.
"""

from __future__ import annotations

import unicodedata

import pytest

from distnsimilarity.core.matching import differing_positions, matches_character_difference
from pins import NamePair, pin_match

# Countable by eye: position numbers are given left to right from 0.
EXACT_DUPLICATE = NamePair("Aba", "Aba")  # 0 differences
ONE_AT_THE_END = NamePair("Aba", "Abe")  # 1: position 2
ONE_IN_THE_MIDDLE = NamePair("Lima", "Loma")  # 1: position 1
TWO_DIFFERENCES = NamePair("Aba", "Aec")  # 2: positions 1 and 2
EVERY_POSITION = NamePair("Aba", "Zed")  # 3: positions 0, 1 and 2
SHUFFLED_SAME_LENGTH = NamePair("Xabc", "abcX")  # 4: an edit distance of 2, 4 positions

# Unequal in length, but reading almost the same: an insertion or deletion apart.
ONE_CHARACTER_LONGER = NamePair("Aba", "Abba")
VOWEL_INSERTED = NamePair("Tara", "Taara")
A_LONGER_SPELLING = NamePair("Genoa", "Geneva")
PLURAL = NamePair("Os", "Oss")

CASE_LOWERED = NamePair("Lyon", "lyon")  # 1: position 0, a case difference only
CASE_UPPERED = NamePair("PARIS", "Paris")  # 4: positions 1-4
ACCENT_FOLDED = NamePair("Genève", "Geneve")  # 1: position 3, `e` folded from `è`
ACCENT_FOLDED_LONG = NamePair("São Paulo", "Sao Paulo")  # 1: position 2
ACCENT_AND_CASE = NamePair("Zürich", "zurich")  # 2: positions 0 and 1


class TestEqualLengthIsRequired:
    @pytest.mark.parametrize(
        "pair", [ONE_CHARACTER_LONGER, VOWEL_INSERTED, A_LONGER_SPELLING, PLURAL]
    )
    @pytest.mark.parametrize("at", [0, 1, 2, 3, 4])
    def test_a_pair_of_unequal_length_never_matches_at_any_threshold(
        self, pair: NamePair, at: int
    ) -> None:
        pin_match(pair, at=at, expected=False)

    def test_an_unequal_pair_is_reported_as_uncomparable_not_as_a_count(self) -> None:
        with pytest.raises(ValueError) as refused:
            differing_positions(ONE_CHARACTER_LONGER.first, ONE_CHARACTER_LONGER.second)
        message = str(refused.value)
        assert "3" in message and "4" in message, message

    @pytest.mark.parametrize(
        "pair,first_length,second_length",
        [(ONE_CHARACTER_LONGER, 3, 4), (A_LONGER_SPELLING, 5, 6)],
    )
    def test_the_refusal_names_both_lengths(
        self, pair: NamePair, first_length: int, second_length: int
    ) -> None:
        with pytest.raises(ValueError) as refused:
            differing_positions(pair.first, pair.second)
        assert f"({first_length})" in str(refused.value)
        assert f"({second_length})" in str(refused.value)


class TestDifferencesArePositional:
    @pytest.mark.parametrize(
        "pair,positions",
        [
            (EXACT_DUPLICATE, 0),
            (ONE_AT_THE_END, 1),
            (ONE_IN_THE_MIDDLE, 1),
            (TWO_DIFFERENCES, 2),
            (EVERY_POSITION, 3),
            (SHUFFLED_SAME_LENGTH, 4),
        ],
    )
    def test_positions_are_counted_one_by_one_without_insertions_or_deletions(
        self, pair: NamePair, positions: int
    ) -> None:
        actual = differing_positions(pair.first, pair.second)
        assert actual == positions, (
            f"{pair}: expected {positions} differing position(s), got {actual} - count them "
            "left to right, one character per position"
        )

    def test_a_shift_is_four_differing_positions_not_two_edits(self) -> None:
        """`Xabc`/`abcX` is two edits apart, and four characters apart. The rule counts
        characters: what lines up with what, position by position."""
        pin_match(SHUFFLED_SAME_LENGTH, at=2, expected=False)
        pin_match(SHUFFLED_SAME_LENGTH, at=4, expected=True)

    def test_an_insertion_is_never_counted_as_one_difference(self) -> None:
        pin_match(ONE_CHARACTER_LONGER, at=1, expected=False)


class TestTheThresholdIsExact:
    @pytest.mark.parametrize(
        "pair,positions",
        [
            (EXACT_DUPLICATE, 0),
            (ONE_AT_THE_END, 1),
            (ONE_IN_THE_MIDDLE, 1),
            (TWO_DIFFERENCES, 2),
            (EVERY_POSITION, 3),
        ],
    )
    def test_a_pair_matches_at_its_own_count_and_at_no_other_count(
        self, pair: NamePair, positions: int
    ) -> None:
        for at in range(0, 5):
            pin_match(pair, at=at, expected=at == positions)

    def test_the_count_is_not_a_minimum(self) -> None:
        """`Aba`/`Zed` differs in 3 positions: asking for 1 does not find it."""
        pin_match(EVERY_POSITION, at=1, expected=False)

    def test_the_count_is_not_a_maximum(self) -> None:
        pin_match(EXACT_DUPLICATE, at=3, expected=False)

    def test_requesting_zero_selects_duplicates_and_nothing_else(self) -> None:
        pin_match(EXACT_DUPLICATE, at=0, expected=True)
        pin_match(ONE_AT_THE_END, at=0, expected=False)
        pin_match(CASE_LOWERED, at=0, expected=False)


class TestComparisonIsCaseSensitive:
    def test_a_case_variant_is_one_differing_character(self) -> None:
        pin_match(CASE_LOWERED, at=1, expected=True)

    def test_a_case_variant_is_not_a_duplicate(self) -> None:
        pin_match(CASE_LOWERED, at=0, expected=False)

    def test_every_folded_letter_counts(self) -> None:
        pin_match(CASE_UPPERED, at=1, expected=False)
        pin_match(CASE_UPPERED, at=4, expected=True)


class TestAccentsAgainstAsciiFoldedSpellings:
    def test_the_fixture_names_are_equal_in_length_as_the_tool_sees_them(self) -> None:
        """One visible letter is one character: no combining marks, or the counts below
        would not be derivable by eye."""
        assert len(ACCENT_FOLDED.first) == len(ACCENT_FOLDED.second) == 6
        assert len(ACCENT_FOLDED_LONG.first) == len(ACCENT_FOLDED_LONG.second) == 9
        for name in ("Genève", "São Paulo", "Zürich"):
            assert name == unicodedata.normalize("NFC", name), f"{name} is not NFC-normalised"

    def test_an_accent_versus_its_folded_letter_is_one_differing_character(self) -> None:
        pin_match(ACCENT_FOLDED, at=1, expected=True)
        pin_match(ACCENT_FOLDED_LONG, at=1, expected=True)

    def test_an_accent_versus_its_folded_letter_is_not_a_duplicate(self) -> None:
        pin_match(ACCENT_FOLDED, at=0, expected=False)

    def test_accent_and_case_differences_count_separately(self) -> None:
        pin_match(ACCENT_AND_CASE, at=1, expected=False)
        pin_match(ACCENT_AND_CASE, at=2, expected=True)


class TestInvalidInputSurfaces:
    """The comparison step used to end in a bare `except: return False`, so a `None` name
    or a string threshold quietly looked like "these two places do not match". Invalid
    input is a failure now."""

    @pytest.mark.parametrize("bad_name", [None, 42, 4.2, ["Aba"], b"Aba"])
    def test_a_name_that_is_not_text_is_refused(self, bad_name: object) -> None:
        with pytest.raises(TypeError) as refused:
            differing_positions(bad_name, "Aba")  # type: ignore[arg-type]
        assert type(bad_name).__name__ in str(refused.value)

    @pytest.mark.parametrize("bad_name", [None, 42, ["Aba"]])
    def test_a_name_that_is_not_text_is_refused_by_the_matcher_too(self, bad_name: object) -> None:
        with pytest.raises(TypeError):
            matches_character_difference(bad_name, "Aba", 1)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_threshold", ["1", 1.0, None, True])
    def test_a_threshold_that_is_not_an_int_is_refused(self, bad_threshold: object) -> None:
        with pytest.raises(TypeError) as refused:
            matches_character_difference("Aba", "Abe", bad_threshold)  # type: ignore[arg-type]
        assert "character difference" in str(refused.value)

    @pytest.mark.parametrize("bad_threshold", [-1, -5])
    def test_a_negative_threshold_is_refused(self, bad_threshold: int) -> None:
        with pytest.raises(ValueError) as refused:
            matches_character_difference("Aba", "Aba", bad_threshold)
        assert str(bad_threshold) in str(refused.value)

    def test_a_refusal_is_not_a_non_match(self) -> None:
        """Nothing about a refusal can be mistaken for a pair that simply did not match."""
        with pytest.raises(TypeError):
            matches_character_difference(None, "Aba", 1)  # type: ignore[arg-type]
