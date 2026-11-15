"""Assertions that pin the frozen core seam.

A pinning failure has to name the pair of place names that was checked, the
character-difference threshold it was checked at, and the expectation that was violated.
That is what makes a broken expectation readable on its own, without opening the test.

The rules being pinned are the ones the original script implemented, including its bare
`except: return False` around the comparison - which is precisely the behaviour that is
*not* carried over: invalid input now raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import differing_positions, matches_character_difference


@dataclass(frozen=True)
class NamePair:
    """Two ASCII names, written the way a failure message names them: `"Abba"/"Aba"`."""

    first: str
    second: str

    def __str__(self) -> str:
        return f'"{self.first}"/"{self.second}"'


@dataclass(frozen=True)
class Spot:
    """A coordinate with a name, so a distance failure says which pair it is."""

    label: str
    latitude: float
    longitude: float

    def __str__(self) -> str:
        return f"{self.label} ({self.latitude}, {self.longitude})"


def _why(pair: NamePair) -> str:
    """The hand-derivable reason a pair does or does not match."""
    if len(pair.first) != len(pair.second):
        return (
            f'"{pair.first}" is {len(pair.first)} characters and "{pair.second}" is '
            f"{len(pair.second)} characters, so the pair is not comparable at any threshold"
        )
    return (
        f"they differ in {differing_positions(pair.first, pair.second)} of "
        f"{len(pair.first)} positions"
    )


def pin_match(pair: NamePair, *, at: int, expected: bool) -> None:
    """Pin whether two ASCII names match at a threshold.

    A pair matches when and only when its count of differing positions equals `at`.
    """
    matched = matches_character_difference(pair.first, pair.second, at)
    if matched is not expected:
        raise AssertionError(
            f'{pair} at n={at}: expected {"a match" if expected else "no match"}, but '
            f"{_why(pair)}, and a pair matches only when the count of differing positions "
            f"equals n exactly - the core reported {'a match' if matched else 'no match'}"
        )


def _unit_hint(actual_km: float, expected_km: float) -> str:
    """Say which unit the mismatch looks like, because a unit change is the usual cause."""
    if not expected_km:
        return ""
    ratio = actual_km / expected_km
    for expected_ratio, unit in ((1000.0, "metres"), (100000.0, "centimetres"), (1.60934, "statute miles")):
        if abs(ratio - expected_ratio) < expected_ratio * 0.01:
            return f" - the reported figure looks like {unit}"
        if abs(ratio - 1.0 / expected_ratio) < 0.01 / expected_ratio:
            return f" - the expected figure looks like {unit}"
    return ""


def pin_distance(*, first: Spot, second: Spot, expected_km: float, tolerance_km: float) -> float:
    """Pin a great-circle distance against a hand-computed figure, in kilometres."""
    actual_km = great_circle_distance_km(
        first.latitude, first.longitude, second.latitude, second.longitude
    )
    if abs(actual_km - expected_km) > tolerance_km:
        hint = _unit_hint(actual_km, expected_km)
        raise AssertionError(
            f"distance {first} -> {second}: expected {expected_km} km "
            f"(+/- {tolerance_km} km), got {actual_km} km{hint}"
        )
    return actual_km


def pin_pairs(
    reported: Iterable[object],
    *,
    at: int,
    expected: Sequence[tuple[str, str]],
) -> None:
    """Pin the exact set of (first asciiname, second asciiname) pairs a scan reported."""
    names = {(pair.first.asciiname, pair.second.asciiname) for pair in reported}
    wanted = set(expected)
    missing = sorted(wanted - names)
    extra = sorted(names - wanted)
    if missing or extra:
        complaints = []
        if missing:
            complaints.append("missing " + ", ".join(f'"{a}"/"{b}"' for a, b in missing))
        if extra:
            complaints.append("unexpected " + ", ".join(f'"{a}"/"{b}"' for a, b in extra))
        raise AssertionError(
            f"scan at n={at} reported the wrong pairs ({'; '.join(complaints)}). "
            f"Expected {len(wanted)} pair(s), got {len(names)}."
        )
