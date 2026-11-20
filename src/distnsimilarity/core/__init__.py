"""The single test seam: candidate selection, name matching, distance, ranking.

Nothing here re-exports `scan` or `matching_pairs` from `distnsimilarity.core.scan`: a name
bound in this package's namespace shadows the submodule of the same name, and the prefilter
tests import `distnsimilarity.core.scan` as a module in order to wrap
`matches_character_difference` and watch what a scan actually asks about. Import those two
from the submodule.
"""

from __future__ import annotations

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import (
    differing_position_indexes,
    differing_positions,
    matches_character_difference,
)
from distnsimilarity.core.places import Place, PlaceRowError, read_places
from distnsimilarity.core.scan import POLL_EVERY_COMPARISONS, CancelCheck, MatchedPair

__all__ = [
    "POLL_EVERY_COMPARISONS",
    "CancelCheck",
    "MatchedPair",
    "Place",
    "PlaceRowError",
    "differing_position_indexes",
    "differing_positions",
    "great_circle_distance_km",
    "matches_character_difference",
    "read_places",
]
