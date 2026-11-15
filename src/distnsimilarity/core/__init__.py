"""The single test seam: candidate selection, name matching, distance, ranking."""

from __future__ import annotations

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import (
    differing_positions,
    matches_character_difference,
)
from distnsimilarity.core.places import Place, read_places

__all__ = [
    "Place",
    "differing_positions",
    "great_circle_distance_km",
    "matches_character_difference",
    "read_places",
]
