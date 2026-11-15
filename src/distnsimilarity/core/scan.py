"""Scanning: walking candidate pairs and reporting the ones that match."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import matches_character_difference
from distnsimilarity.core.places import Place
from distnsimilarity.core.population import above_floor
from distnsimilarity.core.prefilter import EqualLengthGroups

#: The prefilter is reversible on its own: switching it off walks every pair the way the
#: original tool did, and nothing else about a scan changes.
USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT = True


@dataclass(frozen=True)
class MatchedPair:
    """A pair of places that matched, and how far apart they are."""

    first: Place
    second: Place
    distance_km: float


def scan(
    places: list[Place],
    character_difference: int,
    population_floor: int | None = None,
    *,
    equal_length_prefilter: bool = USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT,
) -> Iterator[MatchedPair]:
    """Every matching pair in file order, each unordered pair reported once.

    A place is compared against itself as well, so a name is a pair with itself and, at
    `character_difference=0`, reports a distance of 0 km. That is the behaviour of the
    tool this replaces.

    The population floor is applied while the candidate set is built, so it changes what
    is compared rather than what is displayed.

    With `equal_length_prefilter` on - the default - a place is only ever offered the
    later places whose ASCII name is its own length, which is where the rule is heading
    anyway. It changes how much work a scan does and not which pairs a scan reports:
    both modes yield the same pairs in the same order, as
    `tests/test_equal_length_prefilter.py` proves in both directions.
    """
    candidates = [
        place for place in places if above_floor(place.population, population_floor)
    ]
    groups = EqualLengthGroups.of(candidates) if equal_length_prefilter else None
    # Looked up once, not once per pair: this loop runs billions of times on a big dump.
    matches = matches_character_difference
    distance = great_circle_distance_km
    for index, first in enumerate(candidates):
        first_name = first.asciiname
        partners = (
            groups.partners_from(index) if groups is not None else range(index, len(candidates))
        )
        for position in partners:
            second = candidates[position]
            if matches(first_name, second.asciiname, character_difference):
                yield MatchedPair(
                    first,
                    second,
                    distance(first.latitude, first.longitude, second.latitude, second.longitude),
                )
