"""Name matching: positional character differences between equal-length ASCII names."""

from __future__ import annotations


def differing_positions(first: str, second: str) -> int:
    """How many positions two equal-length ASCII names differ in.

    Differences are counted positionally: no insertion or deletion is allowed, which is
    what makes an unequal pair of names uncomparable rather than a distant match.

    Raises ``ValueError`` when the lengths differ and ``TypeError`` when either name is
    not a string: invalid input surfaces as a failure instead of a silent non-match.
    """
    if not isinstance(first, str) or not isinstance(second, str):
        raise TypeError(
            f"ASCII names must be strings, got {type(first).__name__} and {type(second).__name__}"
        )
    if len(first) != len(second):
        raise ValueError(
            f"Names of different length cannot be compared: {first!r} ({len(first)}) "
            f"and {second!r} ({len(second)})"
        )
    return sum(1 for left, right in zip(first, second) if left != right)


def matches_character_difference(first: str, second: str, character_difference: int) -> bool:
    """True when the names differ in exactly ``character_difference`` positions."""
    if not isinstance(character_difference, int) or isinstance(character_difference, bool):
        raise TypeError(f"character difference must be an int, got {character_difference!r}")
    if character_difference < 0:
        raise ValueError(f"character difference cannot be negative: {character_difference}")
    if not isinstance(first, str) or not isinstance(second, str):
        raise TypeError(
            f"ASCII names must be strings, got {type(first).__name__} and {type(second).__name__}"
        )
    if len(first) != len(second):
        return False
    return differing_positions(first, second) == character_difference
