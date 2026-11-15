"""The equal-length prefilter: names of different length can never be a pair.

The matching rule requires two names to be the same length before it counts anything, so
walking a place against every other place spends most of its effort on pairs that the rule
will reject before it starts. Bucketing candidate places by the length of their ASCII name
removes that effort without removing a single possible pair - which is exactly why this
file is small and its behaviour is pinned by `tests/test_equal_length_prefilter.py`.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from distnsimilarity.core.places import Place


def all_pair_count(places_count: int) -> int:
    """Pairs a full comparison would consider: every place against itself and every
    later place, reported once per unordered pair."""
    return places_count * (places_count + 1) // 2


@dataclass(frozen=True)
class EqualLengthGroups:
    """Candidate positions bucketed by ASCII-name length, in the order the places arrived.

    A place's partners are itself and the later places in its own bucket, so a scan that
    walks buckets sees the same pairs, in the same order, as a scan that walks everything.
    """

    positions_by_length: dict[int, list[int]]
    name_lengths: tuple[int, ...]

    @classmethod
    def of(cls, places: Sequence[Place]) -> "EqualLengthGroups":
        positions_by_length: dict[int, list[int]] = {}
        name_lengths: list[int] = []
        for position, place in enumerate(places):
            length = place.name_length
            name_lengths.append(length)
            positions_by_length.setdefault(length, []).append(position)
        return cls(positions_by_length=positions_by_length, name_lengths=tuple(name_lengths))

    def partners_from(self, position: int) -> Iterator[int]:
        """This position, and every later position whose ASCII name is its own length."""
        group = self.positions_by_length[self.name_lengths[position]]
        yield from group[bisect_left(group, position) :]

    def partner_count_from(self, position: int) -> int:
        """How many partners `partners_from` will hand back, without walking them."""
        group = self.positions_by_length[self.name_lengths[position]]
        return len(group) - bisect_left(group, position)

    def pair_count(self) -> int:
        """Pairs this bucketing considers, over the whole candidate set."""
        return sum(all_pair_count(len(group)) for group in self.positions_by_length.values())

    def __len__(self) -> int:
        return len(self.name_lengths)
