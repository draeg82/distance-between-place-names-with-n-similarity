"""Searching from one place: a range of character differences, and the sweep that walks it.

Everywhere mode asks one question of a whole candidate set - which pairs differ by exactly the
threshold - and answers with the extremes wherever they are. From-a-place mode asks a narrower
question and asks it of every value at once: *this* place, against every place its name could be
confused with, at zero differing characters, at one, at two, up to a limit. The answer is a list
measured from one selected place, plus what each value of the range produced.

Zero is a value like any other here, and it is the reason the range exists: a name shared by two
places is a character difference of zero, and a mode that started at one would put every
duplicate name in the world out of reach. The "exclude identical names" switch is a switch over
what gets *reported* and not over what gets compared - the sweep still counts the identical names
it walked past, and says that it was asked to leave them out. Counting them and hiding the number
would turn a finding into a silence.

This module ranks nothing and reads nothing: it turns one place and an iterable of candidates of
the same name length into pairs, and keeps a tally per value. The candidates, the ranking of the
finds and the distance itself belong to `db.candidates`, `core.topk` and `core.distance`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import differing_position_indexes
from distnsimilarity.core.places import Place
from distnsimilarity.core.scan import (
    POLL_EVERY_COMPARISONS,
    CancelCheck,
    MatchedPair,
    Progress,
    check_monitoring,
)

#: How far up the range a from-a-place search sweeps when nobody says. Three is wide enough to
#: reach the names a place is actually confused with - `Springfield` and `Spring field`s
#: neighbours - without reaching the whole name-length group.
DEFAULT_DIFFERENCE_LIMIT = 3


@dataclass(frozen=True)
class DifferenceRange:
    """The character-difference values a sweep walks over: zero, one, up to `limit`.

    This is not a threshold with a second meaning. Everywhere mode still asks for exactly one
    character difference and nothing else (`core.matching`); a sweep reports a whole range because
    the question being asked of one place is "how many nearly-identical names are there, at each
    degree of nearness", which is a count per value rather than a filter.

    `exclude_identical_names` leaves the value 0 out of the *reported* pairs. It does not shorten
    the sweep, and it does not hide the count: the pairs identical names produced are still
    reported, marked as excluded, because the sweep walked past them either way.
    """

    limit: int = DEFAULT_DIFFERENCE_LIMIT
    exclude_identical_names: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError(
                f"the top of a character-difference range must be a whole number, got {self.limit!r}"
            )
        if self.limit < 0:
            raise ValueError(
                f"a character-difference range cannot reach below zero: {self.limit}"
            )
        if isinstance(self.exclude_identical_names, bool):
            return
        raise TypeError(
            "excluding identical names is a yes or no, got "
            f"{self.exclude_identical_names!r}"
        )

    @property
    def values(self) -> tuple[int, ...]:
        """Every value the range covers, from zero up. Zero is always there."""
        return tuple(range(0, self.limit + 1))

    def within(self, character_difference: int) -> bool:
        """Is a pair with this many differing characters part of the range at all?"""
        return 0 <= character_difference <= self.limit

    def reports(self, character_difference: int) -> bool:
        """Is a pair with this many differing characters one the reader asked to be shown?

        The one place the exclusion is decided. `within` says what the sweep walked into, this says
        what it hands on, and the two differ only at zero - which is the whole point of the switch.
        """
        if not self.within(character_difference):
            return False
        return not (character_difference == 0 and self.exclude_identical_names)

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "exclude_identical_names": self.exclude_identical_names,
        }


@dataclass(frozen=True)
class DifferenceCount:
    """What one value of a range produced: how many pairs, and whether they were reported.

    `excluded` says the pairs at this value are identical names the reader asked not to be shown.
    The count is still the count: a sweep that hid it would be reporting a search it did not do.
    """

    character_difference: int
    pairs: int
    excluded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "character_difference": self.character_difference,
            "pairs": self.pairs,
            "excluded": self.excluded,
        }


class DifferenceCounter:
    """The tally a sweep keeps: how many pairs each value of its range has produced.

    One counter per search, fed by the sweep that owns the range, so the numbers a reader sees next
    to the list came from the same walk as the list. Values are always reported in range order and
    always all of them, including the ones that found nothing: "2: nothing" is a finding, and a
    list that only mentioned the values with pairs would make a wide range look like a narrow one.
    """

    __slots__ = ("difference", "_pairs")

    def __init__(self, difference: DifferenceRange) -> None:
        self.difference = difference
        self._pairs = {value: 0 for value in difference.values}

    def record(self, character_difference: int) -> None:
        """One more pair found at this value. A value outside the range is a bug, not a miss."""
        if character_difference not in self._pairs:
            raise ValueError(
                f"{character_difference} differing characters is outside the range being swept "
                f"(0 to {self.difference.limit})"
            )
        self._pairs[character_difference] += 1

    @property
    def pairs(self) -> int:
        """Every pair found, including the ones at an excluded value."""
        return sum(self._pairs.values())

    def counts(self) -> tuple[DifferenceCount, ...]:
        return tuple(
            DifferenceCount(
                character_difference=value,
                pairs=self._pairs[value],
                excluded=value == 0 and self.difference.exclude_identical_names,
            )
            for value in self.difference.values
        )

    def as_list(self) -> list[dict[str, Any]]:
        return [count.as_dict() for count in self.counts()]


class Sweep:
    """One place, walked against the candidates it could be compared with, over a range.

    The sweep is the from-a-place equivalent of a scan: it holds the origin, the range and the
    tally, and it hands out pairs as it finds them. It counts each pair at the value it matched at
    *before* deciding whether to report it, which is the only way the counts and the list can never
    disagree about how much of the range was walked.

    A place is never a partner of itself. `matching_pairs` reports a place against itself on
    purpose - that is how a threshold of zero reaches every name in everywhere mode - but a
    distance from a place to itself is nothing a search from that place is being asked for.
    """

    __slots__ = ("origin", "difference", "counter")

    def __init__(self, origin: Place, difference: DifferenceRange) -> None:
        if not isinstance(origin, Place):
            raise TypeError(f"a sweep starts from a place, got {type(origin).__name__}")
        if not isinstance(difference, DifferenceRange):
            raise TypeError(
                "a sweep walks a character-difference range, got "
                f"{type(difference).__name__}"
            )
        self.origin = origin
        self.difference = difference
        self.counter = DifferenceCounter(difference)

    @property
    def counts(self) -> tuple[DifferenceCount, ...]:
        """What each value of the range has produced so far, in range order."""
        return self.counter.counts()

    def pairs(
        self,
        partners: Iterable[Place],
        *,
        cancel: CancelCheck | None = None,
        progress: Progress | None = None,
        poll_every_comparisons: int = POLL_EVERY_COMPARISONS,
    ) -> Iterator[MatchedPair]:
        """Every partner whose name is within the range, as it is found.

        Partners are expected to be the same length as the origin's name - the candidate walk that
        hands them over selects on name length, because two names of different length have no
        character difference at all. A partner that isn't is refused the way `differing_positions`
        refuses it, with both names in the message, rather than skipped: a candidate set holding
        the wrong lengths is a bug worth hearing about.

        `cancel` and `progress` are the same cooperative checks a scan takes, and they mean the
        same thing here: a sweep asked to stop ends between two comparisons, keeping the pairs it
        has yielded and the counts it has recorded. They are cheap to pass through and a sweep over
        one name-length group is a few tens of thousands of comparisons, which is long enough to be
        worth stopping.
        """
        difference = self.difference
        counter = self.counter
        origin_name = self.origin.asciiname
        latitude = self.origin.latitude
        longitude = self.origin.longitude
        origin_id = self.origin.geonameid
        monitoring = cancel is not None or progress is not None
        comparisons = 0
        reported = 0
        try:
            for second in partners:
                cancel_requested, progress_delta = check_monitoring(
                    monitoring, progress, comparisons, reported, poll_every_comparisons, cancel
                )
                if cancel_requested:
                    return
                reported += progress_delta
                if second.geonameid == origin_id:
                    # Not a comparison, so not counted as one: this partner is the place itself.
                    continue
                positions = differing_position_indexes(origin_name, second.asciiname)
                character_difference = len(positions)
                if not difference.within(character_difference):
                    comparisons += 1
                    continue
                counter.record(character_difference)
                comparisons += 1
                if not difference.reports(character_difference):
                    continue
                yield MatchedPair(
                    self.origin,
                    second,
                    great_circle_distance_km(
                        latitude, longitude, second.latitude, second.longitude
                    ),
                )
        finally:
            # Same rule as a scan: however the walk ended, what it compared is counted, so a
            # search stopped halfway says how far it got instead of looking like it did nothing.
            if progress is not None and comparisons != reported:
                progress(comparisons - reported)
