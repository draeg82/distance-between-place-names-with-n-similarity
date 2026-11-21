"""Scanning: walking candidate pairs and reporting the ones that match."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Sequence

from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import matches_character_difference
from distnsimilarity.core.places import Place
from distnsimilarity.core.population import above_floor
from distnsimilarity.core.prefilter import EqualLengthGroups, all_pair_count

#: The prefilter is reversible on its own: switching it off walks every pair the way the
#: original tool did, and nothing else about a scan changes.
USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT = True

#: A scan asks its cancellation check this often, in comparisons. A check is a flag read, so
#: this is the difference between stopping in a millisecond and stopping after the next
#: hundred million comparisons; polling every comparison would cost the inner loop.
POLL_EVERY_COMPARISONS = 4_096

#: "Should this scan stop?" - read on the scan's own thread, so the answerer must be
#: thread-safe and must not block.
CancelCheck = Callable[[], bool]

#: Run monitoring (progress reporting and cancellation) at a poll boundary.
#:
#: Reports how many comparisons have happened since the last call and asks
#: whether the loop should stop.
#
#: Returns ``(cancel_requested, progress_delta)`` so the caller can update
#: ``reported`` from the delta and check ``cancel_requested`` to exit.
def check_monitoring(
    monitoring: bool,
    progress: Progress | None,
    comparisons: int,
    reported: int,
    poll_every_comparisons: int,
    cancel: CancelCheck | None,
) -> tuple[bool, int]:
    """Check progress and cancellation at a poll boundary.

    Called at regular intervals inside an inner loop. Reports how many
    comparisons have happened since the last call and asks whether the loop
    should stop.

    Returns a ``(cancel_requested, progress_delta)`` pair. The caller should
    update ``reported`` from the delta and check ``cancel_requested`` to
    decide whether to exit the loop.
    """
    if monitoring and comparisons % poll_every_comparisons == 0:
        delta = 0
        if progress is not None and comparisons != reported:
            delta = comparisons - reported
            progress(delta)
        # Cancel is checked on every poll boundary, regardless of progress.
        if cancel is not None and cancel():
            return True, delta
        return False, delta
    return False, 0

#: "These many more candidate pairs were compared." Called once per poll window rather than
#: once per comparison, so an interface can show a fraction scanned without the inner loop
#: paying for a callback per pair. The total is exact at every window boundary and can lag
#: by up to `poll_every_comparisons` pairs between windows.
#:
#: Unlike `CancelCheck`, a `Progress` **may block**, and that is what it is for: a poll window
#: is the only moment a scan is willing to be held. A caller streaming a scan to a reader uses
#: it to stop the scan at a window boundary until the reader has taken what has been found,
#: which paces the scan to the reader instead of to the machine. A callback that blocks holds up
#: the scan and nothing else, so it must be the caller's own pause and never a lock somebody
#: else is expected to hand back.
Progress = Callable[[int], None]


def candidates_for(
    places: Sequence[Place],
    population_floor: int | None = None,
    *,
    equal_length_prefilter: bool = USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT,
) -> tuple[list[Place], EqualLengthGroups | None]:
    """The candidate list a scan will walk, and the length buckets it walks through.

    Both a scan and a cost estimate come through here, so the number a search quotes before
    it starts cannot disagree with the work it then does.
    """
    candidates = [place for place in places if above_floor(place.population, population_floor)]
    groups = EqualLengthGroups.of(candidates) if equal_length_prefilter else None
    return candidates, groups


def candidate_scope(
    places: Sequence[Place],
    population_floor: int | None = None,
    *,
    equal_length_prefilter: bool = USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT,
) -> tuple[int, int]:
    """How many places a scan compares, and how many pairs that comes to.

    This is what a search quotes as its cost before it starts, so it is counted from the same
    candidate list the scan walks rather than from a second rule that can drift from it.
    """
    candidates, groups = candidates_for(
        places, population_floor, equal_length_prefilter=equal_length_prefilter
    )
    if groups is None:
        return len(candidates), all_pair_count(len(candidates))
    return len(candidates), groups.pair_count()


@dataclass(frozen=True)
class MatchedPair:
    """A pair of places that matched, and how far apart they are."""

    first: Place
    second: Place
    distance_km: float


def matching_pairs(
    first: Place,
    partners: Iterable[Place],
    character_difference: int,
    *,
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
    poll_every_comparisons: int = POLL_EVERY_COMPARISONS,
) -> Iterator[MatchedPair]:
    """One place against its possible partners - the inner loop of every scan.

    Both candidate paths come through here, so there is exactly one place where a pair of
    places becomes a reported pair. Partners are consumed as they arrive: nothing here
    holds a whole candidate set in memory.

    With a `cancel` check, the walk ends between two comparisons as soon as it says yes:
    the pairs already yielded stand, and nothing after them is compared. A scan that stops
    this way is not an error, so no exception is raised - the caller already knows why it
    stopped.

    With a `progress` callback, it is told how many comparisons have happened - once per
    poll window, at the same moment the scan thinks about stopping.
    """
    # Looked up once, not once per pair: this loop runs billions of times on a big dump.
    matches = matches_character_difference
    distance = great_circle_distance_km
    first_name = first.asciiname
    first_latitude = first.latitude
    first_longitude = first.longitude
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
            comparisons += 1
            if matches(first_name, second.asciiname, character_difference):
                yield MatchedPair(
                    first,
                    second,
                    distance(
                        first_latitude, first_longitude, second.latitude, second.longitude
                    ),
                )
    finally:
        # Whatever ended the walk - the end, a cancellation, a reader going away - the
        # comparisons that happened are counted, so a stopped search still says how far it got.
        if progress is not None and comparisons != reported:
            progress(comparisons - reported)


def scan(
    places: list[Place],
    character_difference: int,
    population_floor: int | None = None,
    *,
    equal_length_prefilter: bool = USE_EQUAL_LENGTH_PREFILTER_BY_DEFAULT,
    cancel: CancelCheck | None = None,
    progress: Progress | None = None,
    poll_every_comparisons: int = POLL_EVERY_COMPARISONS,
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

    `cancel` and `progress` do for the whole scan what they do for one place's partners;
    the scan also thinks about stopping between candidate places, so a scan that is asked to
    stop does not have to finish a name-length group to hear about it.
    """
    candidates, groups = candidates_for(
        places, population_floor, equal_length_prefilter=equal_length_prefilter
    )
    for index, first in enumerate(candidates):
        partners = (
            groups.partners_from(index) if groups is not None else range(index, len(candidates))
        )
        if cancel is not None and cancel():
            return
        yield from matching_pairs(
            first,
            (candidates[position] for position in partners),
            character_difference,
            cancel=cancel,
            progress=progress,
            poll_every_comparisons=poll_every_comparisons,
        )
