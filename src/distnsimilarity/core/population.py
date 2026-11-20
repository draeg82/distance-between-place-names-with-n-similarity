"""The population floor: the minimum population a place may have to be compared."""

from __future__ import annotations


def above_floor(population: int, population_floor: int | None) -> bool:
    """True when a place survives a population floor.

    A place sitting exactly on the floor survives: a floor of 5,000 includes the places
    with a population of exactly 5,000.
    """
    if population_floor is None:
        return True
    return population >= population_floor
