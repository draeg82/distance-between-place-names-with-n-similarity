"""The running top-K: what "the best so far" means, and when it counts as having improved.

A scan streams pairs while a client watches a list get better as it goes. Two questions have
to be answered exactly, or a browser and the core will disagree about what the list holds:

* **Which pairs are the best** - greatest distance first, or least, with a tie-break that
  does not depend on the order the pairs happened to arrive in.
* **When the list has improved** - a snapshot is only worth sending when the ranked set
  actually changed, which is what lets the transport coalesce improvements rather than flood
  a client with one message per pair.

This module never computes a distance: it ranks the distances the scan hands it. It does
count differing characters, because that is a property of the two names rather than of the
search, and a reported pair that disagreed with its own names would be worse than useless.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator

from distnsimilarity.core.matching import differing_position_indexes, differing_positions
from distnsimilarity.core.places import Place
from distnsimilarity.core.scan import MatchedPair

#: How many of the best finds a client watches by default. Fifty rows is a screenful of
#: extremes in both directions and still a small enough payload to send several times a second.
DEFAULT_TOP_K = 50


class Ranking(str, Enum):
    """Which end of the distance range counts as best."""

    GREATEST = "greatest"
    LEAST = "least"

    def __str__(self) -> str:
        return self.value


def parse_direction(value: object) -> Ranking:
    """Read a direction from whatever a client sent: the word, in any case, or the member.

    An unknown spelling is refused by naming both what arrived and what exists, because a
    browser that sends `nearest` needs to be able to see its own mistake.
    """
    if isinstance(value, Ranking):
        return value
    if isinstance(value, str):
        try:
            return Ranking(value.strip().lower())
        except ValueError:
            pass
    known = ", ".join(direction.value for direction in Ranking)
    raise ValueError(f"unknown ranking direction {value!r}: expected one of {known}")


@dataclass(frozen=True)
class RankedPair:
    """A reported pair in the ranking, with what a client needs to display it.

    The count of differing characters is what a list row reports and what a threshold is
    compared against; the positions are what an interface highlights. Both are derived from
    the two names here rather than passed in, so every path that ranks a pair - command line,
    HTTP, a browser - reports the same thing.
    """

    first: Place
    second: Place
    distance_km: float
    character_difference: int
    differing_position_indexes: tuple[int, ...]

    @classmethod
    def of(cls, matched: MatchedPair) -> "RankedPair":
        return cls(
            first=matched.first,
            second=matched.second,
            distance_km=matched.distance_km,
            character_difference=differing_positions(
                matched.first.asciiname, matched.second.asciiname
            ),
            differing_position_indexes=differing_position_indexes(
                matched.first.asciiname, matched.second.asciiname
            ),
        )


class TopK:
    """The best `limit` pairs seen so far, in the order a client should display them.

    A full list only changes when something arrives strictly better than its worst entry. A
    pair that merely ties the worst entry is refused: without that rule a long scan over a
    thousand equal distances would resend the same list a thousand times.
    """

    __slots__ = ("limit", "direction", "_entries")

    def __init__(self, limit: int, direction: Ranking = Ranking.GREATEST) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"top_k must be a whole number of pairs, got {limit!r}")
        if limit < 1:
            raise ValueError(f"top_k must be at least 1 pair, got {limit}")
        self.limit = limit
        self.direction = parse_direction(direction)
        self._entries: list[tuple[tuple[float, int, int], RankedPair]] = []

    @classmethod
    def of(cls, stream: Iterable[MatchedPair], limit: int, direction: Ranking) -> "TopK":
        """A ranking holding the whole of `stream`."""
        top = cls(limit, direction)
        for matched in stream:
            top.add(matched)
        return top

    @property
    def full(self) -> bool:
        return len(self._entries) == self.limit

    @property
    def size(self) -> int:
        return len(self._entries)

    def _key(self, ranked: RankedPair) -> tuple[float, int, int]:
        distance = ranked.distance_km
        if self.direction is Ranking.GREATEST:
            distance = -distance
        # Equal distances are ordered by geoname id, so the list a client sees does not
        # depend on the order the scan happened to find them in.
        return (distance, ranked.first.geonameid, ranked.second.geonameid)

    def add(self, matched: MatchedPair) -> bool:
        """Offer a pair; True when the list is now better than it was."""
        return self.add_ranked(RankedPair.of(matched))

    def add_ranked(self, ranked: RankedPair) -> bool:
        key = self._key(ranked)
        if len(self._entries) < self.limit:
            self._insert(key, ranked)
            return True
        if key >= self._entries[-1][0]:
            return False
        self._insert(key, ranked)
        self._entries.pop()
        return True

    def _insert(self, key: tuple[float, int, int], ranked: RankedPair) -> None:
        # Ranked on the key alone: two entries can share a key only if the same pair was
        # offered twice, and RankedPair itself has no ordering to fall back on.
        bisect.insort(self._entries, (key, ranked), key=lambda entry: entry[0])

    def snapshot(self) -> tuple[RankedPair, ...]:
        """The current list, best first, as a tuple nobody can change."""
        return tuple(ranked for _, ranked in self._entries)


def snapshots(
    stream: Iterable[MatchedPair],
    *,
    limit: int = DEFAULT_TOP_K,
    direction: Ranking = Ranking.GREATEST,
) -> Iterator[tuple[RankedPair, ...]]:
    """The ranking's improving states as `stream` is consumed - one snapshot per improvement.

    This is the sequence a client watches, and it is reproducible: the same pair stream in a
    different search produces the same snapshots, because nothing here depends on wall-clock
    time or on how the pairs were found.
    """
    top = TopK(limit, direction)  # refused before the first pair, not halfway through
    for matched in stream:
        if top.add(matched):
            yield top.snapshot()
