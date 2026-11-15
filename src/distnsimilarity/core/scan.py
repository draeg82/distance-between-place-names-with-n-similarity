"""Scanning: walking candidate pairs and reporting the ones that match."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import matches_character_difference
from distnsimilarity.core.places import Place
from distnsimilarity.core.population import above_floor


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
) -> Iterator[MatchedPair]:
    """Every matching pair in file order, each unordered pair reported once.

    A place is compared against itself as well, so a name is a pair with itself and, at
    `character_difference=0`, reports a distance of 0 km. That is the behaviour of the
    tool this replaces.

    The population floor is applied while the candidate set is built, so it changes what
    is compared rather than what is displayed.
    """
    candidates = [
        place for place in places if above_floor(place.population, population_floor)
    ]
    for index, first in enumerate(candidates):
        for second in candidates[index:]:
            if matches_character_difference(
                first.asciiname, second.asciiname, character_difference
            ):
                yield MatchedPair(
                    first,
                    second,
                    great_circle_distance_km(
                        first.latitude, first.longitude, second.latitude, second.longitude
                    ),
                )
