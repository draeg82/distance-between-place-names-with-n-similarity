"""The search seam: opening a search once, and stopping one on purpose.

Both adapters - the command line and the HTTP API - call ``open_search`` and iterate
``SearchSession.pairs``. There is no second scan and no second matching module: what a
headless run reports is what the same request over HTTP reports, pair for pair, in the same
order. `tests/test_search_seam.py` pins that; `tests/test_http_search.py` checks the HTTP
adapter actually goes through here.

Cancellation is cooperative and shared. A search is handed a ``SearchControl``; the scan
reads it on its own thread every `poll_every_comparisons` comparisons, and an adapter
cancels it from wherever the client went away. Nothing is killed mid-comparison, and a
cancelled search ends where the cancellation landed rather than part-way through a pair.

There is one search source: the place database built by ``distnsimilarity-import``. A search
that cannot read it says so rather than quietly reading something else, because a dump file and
a database imported from a different dump give different answers to the same question.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from distnsimilarity.core.scan import POLL_EVERY_COMPARISONS, MatchedPair
from distnsimilarity.core.topk import DEFAULT_TOP_K, RankedPair, Ranking, TopK, parse_direction
from distnsimilarity.datasets import (
    DatasetNotAvailableError,
    WHOLE_DATABASE_LABEL,
    dataset_label,
    parse_dataset_threshold,
)
from distnsimilarity.db.candidates import (
    dataset_options,
    places_at_least,
    scan_database,
    scope_for,
)
from distnsimilarity.db.schema import connect, default_database_path, place_count

#: Pairs pulled off a search per step when an adapter advances it in the background. A
#: search only runs while somebody is reading it, which is what keeps a client that has
#: gone away from costing any more work.
DEFAULT_BATCH_SIZE = 64


class InvalidSearchRequest(ValueError):
    """A request that cannot describe a search: a dataset naming no threshold, a negative `n`,
    or a bad floor."""


class CacheNotImportedError(RuntimeError):
    """A search was asked for before the place cache held any places.

    This is the one thing that stops a server from searching at all, so it says what to run.
    """


@dataclass(frozen=True, kw_only=True)
class SearchRequest:
    """What to search: which dataset, which floor, and which candidate rule.

    There is one source - the place cache built by ``distnsimilarity-import`` - so a request
    names a *dataset* rather than a file: a population threshold (``"5000"``) or a dump name
    (``"cities5000.txt"``), both meaning "cities of 5,000 or more people", or nothing at all to
    mean every place in the cache. `min_population` stays a separate parameter that keeps its
    own meaning: the caller's own number, applied before matching. Both are floors on the
    population of a *candidate*, so giving both means the stricter of the two and neither
    trims the results.

    `top_k` and `direction` say what "the best so far" means for this search. They belong to
    the request rather than to an adapter because the ranking is the core's: a browser, a
    command-line watcher and a CSV all rank a pair the same way or they disagree in public.
    """

    n: int
    dataset: str | None = None
    min_population: int | None = None
    equal_length_prefilter: bool = True
    database: Path | None = None
    top_k: int = DEFAULT_TOP_K
    direction: Ranking = Ranking.GREATEST
    #: The population threshold the dataset resolved to, or None for the whole cache. Filled in
    #: by `__post_init__`, so a search reads a number instead of re-parsing a spelling.
    dataset_threshold: int | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # The dataset is resolved here, before any pair, so an adapter answers a dataset nobody
        # would search with a refusal rather than a stream of nothing.
        try:
            threshold = (
                None if self.dataset is None else parse_dataset_threshold(self.dataset)
            )
        except DatasetNotAvailableError as error:
            raise InvalidSearchRequest(str(error)) from error
        object.__setattr__(self, "dataset_threshold", threshold)
        if isinstance(self.n, bool) or not isinstance(self.n, int):
            raise InvalidSearchRequest(
                f"the character difference must be a whole number, got {self.n!r}"
            )
        if self.n < 0:
            raise InvalidSearchRequest(
                f"the character difference cannot be negative: {self.n}"
            )
        if self.min_population is not None:
            if isinstance(self.min_population, bool) or not isinstance(self.min_population, int):
                raise InvalidSearchRequest(
                    f"the population floor must be a whole number, got {self.min_population!r}"
                )
            if self.min_population < 0:
                raise InvalidSearchRequest(
                    f"the population floor cannot be negative: {self.min_population}"
                )
        try:
            object.__setattr__(self, "direction", parse_direction(self.direction))
        except ValueError as error:
            raise InvalidSearchRequest(str(error)) from error
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise InvalidSearchRequest(
                f"the number of best pairs must be a whole number, got {self.top_k!r}"
            )
        if self.top_k < 1:
            raise InvalidSearchRequest(
                f"a search has to keep at least one of its best pairs, got {self.top_k}"
            )

    @property
    def database_path(self) -> Path:
        """The cache this search reads, defaulted the way every other entry point defaults it."""
        return default_database_path() if self.database is None else Path(self.database)

    @property
    def dataset_name(self) -> str:
        """What the dataset choice means, in the words the dropdown would have used."""
        if self.dataset_threshold is None:
            return WHOLE_DATABASE_LABEL
        return dataset_label(int(self.dataset_threshold))


class SearchControl:
    """The one way a running search is steered from outside: told to stop, or held.

    ``cancel`` is safe from any thread and any number of times; the first reason given is
    the one that is kept, because it is the one that actually stopped the work. The scan
    notices on its own thread, so a search that is cancelled keeps its current pair and
    does not produce another.

    ``pacing`` is the other half of steering: a caller that cannot take the whole scan at once
    - an HTTP response writing to a socket, say - sets it to a callable that waits, and the
    scan consults it at the same poll boundaries where it thinks about stopping. It is a seam
    for one search's own driver, so it may block the scan and nothing else; see
    `core.scan.Progress` for why a poll window is the only place that can hold a scan.
    """

    def __init__(self, *, poll_every_comparisons: int = POLL_EVERY_COMPARISONS) -> None:
        if poll_every_comparisons < 1:
            raise ValueError(
                f"poll_every_comparisons must be at least 1, got {poll_every_comparisons}"
            )
        self.poll_every_comparisons = int(poll_every_comparisons)
        self._stop = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()
        #: How many times the scan has asked whether to stop. One poll stands for
        #: `poll_every_comparisons` comparisons, so this is how a reader can see how much of
        #: a scan actually happened - including after the search is over.
        self._polls = 0
        #: Set by whoever is driving this search, if anybody is pacing it. Read on the scan's
        #: thread, so a pacing call may block that scan - and must not block anything else.
        self.pacing: Callable[[], None] | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if not self._reason:
                self._reason = reason or "cancelled"
        self._stop.set()

    def stopped(self) -> bool:
        """The `CancelCheck` the scan reads, on the search thread."""
        # One writer per search, and the answer does not have to be exact to be useful.
        self._polls += 1
        return self._stop.is_set()

    def hold(self) -> None:
        """Give the caller driving this search a chance to hold it, on the search thread.

        Called at a poll boundary, where the scan is already prepared to be told to stop, and
        never after a stop has been asked for: a search that has been cancelled is on its way
        out and must not be made to wait for a reader that has gone away.
        """
        pacing = self.pacing
        if pacing is not None and not self._stop.is_set():
            pacing()

    @property
    def polls(self) -> int:
        return self._polls

    @property
    def cancelled(self) -> bool:
        return self._stop.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason


@dataclass(frozen=True)
class CostEstimate:
    """What a search is going to cost, said before it commits to running.

    `candidate_pairs` is exact: it is counted from the same candidate list the scan walks.
    `seconds` is not - the only honest source of one is a rate measured on this machine from
    a search that really ran, and until there is one the estimate says so instead of
    guessing. `basis` is what an interface puts next to the number.
    """

    candidate_pairs: int
    comparisons_per_second: float | None = None
    seconds: float | None = None
    basis: str = "not measured on this machine yet"

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_pairs": self.candidate_pairs,
            "comparisons_per_second": self.comparisons_per_second,
            "seconds": None if self.seconds is None else round(self.seconds, 3),
            "basis": self.basis,
        }


class MeasuredRate:
    """The last comparisons-per-second this process really managed.

    One machine's scan rate is not another's, and a figure copied from a benchmark nobody ran
    is a guess with a decimal point in it. So an estimate is built from a search that has
    actually run here, and says when there has not been one yet.
    """

    def __init__(self) -> None:
        self._per_second: float | None = None
        self._lock = threading.Lock()

    @property
    def comparisons_per_second(self) -> float | None:
        with self._lock:
            return self._per_second

    def record(self, comparisons: int, seconds: float) -> None:
        """Remember what a stretch of scanning just cost. Nothing measured is thrown away."""
        if comparisons <= 0 or seconds <= 0.0:
            return
        with self._lock:
            self._per_second = comparisons / seconds

    def estimate(self, candidate_pairs: int) -> CostEstimate:
        rate = self.comparisons_per_second
        if rate is None:
            return CostEstimate(candidate_pairs=candidate_pairs)
        return CostEstimate(
            candidate_pairs=candidate_pairs,
            comparisons_per_second=rate,
            seconds=candidate_pairs / rate,
            basis="measured from an earlier search on this server",
        )


@dataclass
class SearchStats:
    """What a search has done so far, and how it ended.

    `candidate_pairs` is the whole job and `pairs_compared` is the part of it that has
    happened, which is what an interface turns into a fraction and a time remaining. Both
    are counted in comparisons, not in results: a scan that has found nothing has still
    worked.
    """

    places: int = 0
    population_floor: int = 0
    dataset_threshold: int | None = None
    dataset_label: str = ""
    database_path: str = ""
    candidate_pairs: int = 0
    pairs_compared: int = 0
    pairs_emitted: int = 0
    improvements: int = 0
    comparison_polls: int = 0
    completed: bool = False
    cancelled: bool = False
    cancel_reason: str = ""
    error: str = ""
    finished: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def share_compared(self) -> float | None:
        """How far through the candidate pairs this search is, or None if nothing is known."""
        if self.candidate_pairs <= 0:
            return None
        return min(1.0, self.pairs_compared / self.candidate_pairs)

    def rate_per_second(self) -> float | None:
        """What a comparison has cost this search, or None while that is not worth quoting.

        Only a search that ran to the end counts: a scan that was stopped part way through
        spent most of its time doing other things, and quoting it as the cost of searching
        would make every later estimate too optimistic.
        """
        if not self.completed or self.pairs_compared <= 0:
            return None
        elapsed = self.elapsed_seconds()
        return None if elapsed <= 0.0 else self.pairs_compared / elapsed

    @property
    def retryable(self) -> bool:
        """Would running this search again be the point?

        A search that was stopped before it finished left work undone, so yes - that is what a
        client that hung up, or a watcher that pressed Ctrl-C, is being told. A search that
        walked all its candidates has nothing left to retry, and one that failed needs whatever
        broke fixing first.
        """
        return self.cancelled and not self.error

    def as_dict(self) -> dict[str, object]:
        return {
            "places": self.places,
            "population_floor": self.population_floor,
            # `dataset` is the number a request would have sent (or null for the whole cache) and
            # `dataset_label` the words for it: one for a client to filter on, one for it to show.
            "dataset": self.dataset_threshold,
            "dataset_label": self.dataset_label,
            "database": self.database_path,
            "candidate_pairs": self.candidate_pairs,
            "pairs_compared": self.pairs_compared,
            "pairs": self.pairs_emitted,
            "improvements": self.improvements,
            "comparison_polls": self.comparison_polls,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "comparisons_per_second": self.rate_per_second(),
            "share_compared": (
                None
                if self.share_compared() is None
                else round(self.share_compared() or 0.0, 4)
            ),
            "completed": self.completed,
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason or None,
            "retryable": self.retryable,
            "error": self.error or None,
        }


@dataclass
class SearchSession:
    """One open search: where the places came from, the pairs it reports, and the best of
    them so far.

    The ranking belongs to the session, not to an adapter. Every pair passes through it as it
    is handed out, so the top-K a browser watches is the top-K the same search writes to a
    CSV, and the snapshot sequence is reproducible from the pair stream alone.
    """

    request: SearchRequest
    database_path: Path
    control: SearchControl
    stats: SearchStats
    _pairs: Iterator[MatchedPair]
    _connection: sqlite3.Connection | None = field(default=None, repr=False)
    _iterator: Iterator[MatchedPair] | None = field(default=None, repr=False)
    _ranker: TopK = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._ranker = TopK(self.request.top_k, self.request.direction)

    @property
    def name(self) -> str:
        """What to call what is being searched, in a message or a `started` event.

        It is the dataset rule, not a file name: the cache holds places imported from any
        number of dumps, so naming one of them would be a guess about what was searched.
        """
        return self.request.dataset_name

    @property
    def limit(self) -> int:
        return self._ranker.limit

    @property
    def direction(self) -> Ranking:
        return self._ranker.direction

    def snapshot(self) -> tuple[RankedPair, ...]:
        """The best pairs found so far, in the order a client should show them."""
        return self._ranker.snapshot()

    @property
    def pairs(self) -> Iterator[MatchedPair]:
        """The pairs, counted as they go, with the ending recorded when it comes.

        One iterator per session, handed out again and again: an adapter that pulls it in
        batches and an adapter that walks it to the end are walking the same search.
        """
        return self.iterator()

    def iterator(self) -> Iterator[MatchedPair]:
        if self._iterator is None:
            self._iterator = self._counted()
        return self._iterator

    def _counted(self) -> Iterator[MatchedPair]:
        try:
            for match in self._pairs:
                self.stats.pairs_emitted += 1
                self.stats.comparison_polls = self.control.polls
                if self._ranker.add(match):
                    self.stats.improvements += 1
                yield match
            # A scan that stopped because it was told to stop did not cover its candidates,
            # so "completed" is not the end of the iterator - it is the end without a stop.
            if not self.control.cancelled:
                self.stats.completed = True
        except GeneratorExit:
            # The reader gave up on the search. That is a way of ending, not a failure, and
            # saying otherwise would turn every closed connection into a reported error.
            raise
        except BaseException as error:
            self.stats.error = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.stats.cancelled = self.control.cancelled
            self.stats.cancel_reason = self.control.reason
            self.stats.comparison_polls = self.control.polls
            self.stats.finished = True
            # The scan's own last comparisons are counted here rather than whenever the
            # garbage collector notices the abandoned generator: a stopped search says how far
            # it got at the moment it stopped.
            close_scan = getattr(self._pairs, "close", None)
            if close_scan is not None:
                close_scan()
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def close(self) -> None:
        """Give up on the search: close the iterator (running its ending) and the cache."""
        close = getattr(self._iterator, "close", None)
        if close is not None:
            close()
        else:
            # Nothing pulled this search: close the scan itself so it counts what it compared.
            close_scan = getattr(self._pairs, "close", None)
            if close_scan is not None:
                close_scan()
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def open_search(
    request: SearchRequest,
    *,
    control: SearchControl | None = None,
) -> SearchSession:
    """Open the search a request describes, before any pair has been compared.

    The place database is the only thing a search reads, and opening it is the only way a
    search can fail before a pair exists: ``InvalidSearchRequest`` for a dataset that names no
    threshold, ``DatasetNotAvailableError`` for one this database cannot honour, and
    ``CacheNotImportedError`` for a database with no places in it. All three are answered before
    the scan starts, which is what lets an adapter reply with a status code.
    """
    search_control = SearchControl() if control is None else control
    database = request.database_path.expanduser()
    # Read before `connect`, which creates the file: a search pointed at the wrong path should
    # say "no database here", not leave an empty one behind to confuse the next caller.
    present = database.is_file()
    connection = connect(database)
    if place_count(connection) == 0:
        connection.close()
        here = (
            f"there is no place database at {database}"
            if not present
            else f"the place database at {database} has no places in it"
        )
        raise CacheNotImportedError(
            f"{here}, and the place database is the only search source: run "
            "distnsimilarity-import to fill it from a GeoNames dump"
        )
    if request.dataset_threshold is not None:
        # A dataset is a rule about the places that exist, so a rule this cache cannot honour is
        # refused with the rules it can - the same list the dropdown is built from, so the answer
        # and the choice a person was looking at cannot disagree.
        if places_at_least(connection, int(request.dataset_threshold)) == 0:
            choices = ", ".join(option.label for option in dataset_options(connection))
            connection.close()
            raise DatasetNotAvailableError(
                f"no place in {database} has {int(request.dataset_threshold):,} or more people; "
                f"this database can be searched as: {choices}"
            )
    scope = scope_for(
        connection,
        request.min_population,
        dataset_threshold=request.dataset_threshold,
    )
    stats = SearchStats(
        places=scope.places,
        population_floor=scope.population_floor,
        dataset_threshold=request.dataset_threshold,
        dataset_label=request.dataset_name,
        database_path=str(database.resolve()),
        candidate_pairs=(
            scope.pair_count if request.equal_length_prefilter else scope.full_pair_count
        ),
    )
    pairs = scan_database(
        connection,
        request.n,
        request.min_population,
        dataset_threshold=request.dataset_threshold,
        equal_length_prefilter=request.equal_length_prefilter,
        cancel=search_control.stopped,
        progress=_count_comparisons(stats, search_control),
        poll_every_comparisons=search_control.poll_every_comparisons,
    )
    return SearchSession(
        request=request,
        database_path=database.resolve(),
        control=search_control,
        stats=stats,
        _pairs=pairs,
        _connection=connection,
    )


def _count_comparisons(
    stats: SearchStats, control: SearchControl
) -> Callable[[int], None]:
    """The scan's progress callback: add to the count, then let the driver hold the scan.

    It runs on the search thread, once per poll window, so an interface can quote a fraction
    scanned and a rate without the inner loop paying per comparison. Whether it blocks is the
    driver's business, not the scan's: with no `pacing` set it counts and returns, and with one
    it is where a caller that cannot take a whole scan at once catches up.
    """

    def report(comparisons: int) -> None:
        stats.pairs_compared += comparisons
        control.hold()

    return report

