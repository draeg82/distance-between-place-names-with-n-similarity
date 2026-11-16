"""Running a search from the event loop: one long pull, released slice by slice.

The scan is a synchronous generator. Put it straight in an ASGI response handler and it
either blocks the event loop - so a second client, and the health endpoint, wait for the first
search to finish - or it gets a thread of its own, so every open search is a thread. Neither.
So the scan is walked by one pull on a bounded worker pool, and that pull is *held* at the
poll boundaries the scan already has.

Holding at a poll boundary is what makes a slow search streamable. A pull that only ever
stopped when it had filled a batch would sit inside a rare-match scan for minutes before
writing a line, because "found a pair" and "did some work" are not the same thing: matches can
be so rare that a whole 300,000-place dump yields two pairs. A poll window is the only moment a
scan is willing to be held (`core.scan.Progress` is documented as allowed to block for exactly
this reason), so the pull stops there whenever its slice is full or its time is up, tells the
event loop, and waits to be let go. The scan then runs at the reader's pace rather than at the
machine's, which is also what keeps a client that has stopped reading from becoming either
memory growth or a search nobody is watching.

That hold is also the cancellation story, and it works in both directions. A reader that goes
away means no more slices are taken, so the pull is held until it is cancelled, and a cancelled
pull stops parking so it reaches the scan's own stop check within one poll window instead of
walking out the rest of the file. Because a search only advances while somebody is reading it,
a pull cannot run ahead of the socket that is reporting it.

There is one exception, and it is deliberate: the pull never closes the door behind itself
until it has handed its last slice over, so the tail of a scan is never dropped, and the pull
owns closing the session whether it walked to the end, was cancelled, or died. Only one scan
runs per process (the registry decides that), so a worker parked while the loop writes is
backpressure and not a leak.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from distnsimilarity.core.scan import MatchedPair
from distnsimilarity.search import DEFAULT_BATCH_SIZE, SearchSession, SearchStats

#: Worker threads shared by the searches on this server. Only one scan runs at a time (the
#: registry decides that), so this is not a ceiling on clients: it is one thread to walk the
#: scan and one to open a dataset or price a search while that scan holds the other.
DEFAULT_SEARCH_WORKERS = 2

#: How long a slice may take before the pull offers it to the reader. This is how long a pair
#: can wait behind a stretch of scan that finds nothing, and how finely a long scan is chopped
#: up for progress: 20 ms is far below what a reader notices and far above the cost of a
#: handoff. A slice only ever ends at a poll boundary, so this is a target, not a deadline.
DEFAULT_STEP_SECONDS = 0.02

#: How long the event loop waits for a slice before looking again at the pull. A parked pull
#: wakes the loop itself, so this is only how long a wedged producer can go unnoticed; it is
#: generous so that a slow poll boundary on a big name-length group is never mistaken for one.
DEFAULT_PATIENCE_SECONDS = 5.0


@dataclass(frozen=True)
class Step:
    """What one slice produced, and whether the search has more to give."""

    pairs: list[MatchedPair]
    exhausted: bool

    @property
    def finished(self) -> bool:
        return self.exhausted and not self.pairs


@dataclass
class SliceBudget:
    """When the pull has done enough for one slice.

    Two ways to spend it, because a slice that waits for pairs is a slice that says nothing
    when there are no pairs to say: a full batch of finds, or a stretch of time. Both are
    checked at a poll boundary, which is the only place a scan can be held.
    """

    batch_size: int
    step_seconds: float
    collected: list[MatchedPair] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
        if self.step_seconds <= 0:
            raise ValueError(f"step_seconds must be positive, got {self.step_seconds}")

    @property
    def spent(self) -> bool:
        return (
            len(self.collected) >= self.batch_size
            or time.monotonic() - self.started_at >= self.step_seconds
        )

    def take(self) -> list[MatchedPair]:
        """Hand the slice over and start the next one's clock here, not on the pull thread.

        The clock starts when the reader takes the slice, so time a search spent waiting for a
        slow reader is not charged to the scan that follows it. Taking a slice is what releases
        a parked pull, so it happens exactly once per slice - see `SearchRunner.next_step`.
        """
        pairs = self.collected
        self.collected = []
        self.started_at = time.monotonic()
        return pairs


@dataclass
class SearchPool:
    """The worker threads every search on this server shares."""

    workers: int = DEFAULT_SEARCH_WORKERS
    batch_size: int = DEFAULT_BATCH_SIZE
    step_seconds: float = DEFAULT_STEP_SECONDS
    patience_seconds: float = DEFAULT_PATIENCE_SECONDS
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError(f"workers must be at least 1, got {self.workers}")
        if self.step_seconds <= 0:
            raise ValueError(f"step_seconds must be positive, got {self.step_seconds}")
        if self.patience_seconds <= 0:
            raise ValueError(f"patience_seconds must be positive, got {self.patience_seconds}")

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="search"
            )
        return self._executor

    def start(self, session: SearchSession) -> "SearchRunner":
        return SearchRunner(session=session, pool=self)

    def close(self) -> None:
        """Stop taking work. Anything still running has been cancelled by its own request."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


class SearchRunner:
    """One open search: one pull of the scan, parked at poll boundaries for the reader.

    The loop talks to `next_step` and `cancel`; the pull thread talks to `_pace`. Everything
    they share is behind one condition, and the scan generator itself is only ever touched by
    the pull thread - so there is no lock to hold across a close and no second owner of the
    session's cache connection.
    """

    def __init__(self, session: SearchSession, pool: SearchPool) -> None:
        self.session = session
        self._pool = pool
        self._budget = SliceBudget(
            batch_size=pool.batch_size, step_seconds=pool.step_seconds
        )
        self._condition = threading.Condition()
        self._ready = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._pulling = False
        #: Every time the pull parks at a boundary it raises `_parked`; every time the loop
        #: takes a slice it catches `_taken` up to it. A slice is therefore handed over exactly
        #: once, however often the loop asks, and one parked pull cannot be read twice.
        self._parked = 0
        self._taken = 0
        #: The best-so-far list as of the last slice handed over. A change in it is worth a
        #: stop of its own, whether or not this slice is otherwise full.
        self._improvements_handed_over = 0
        self._exhausted = False
        self._error: BaseException | None = None
        self._finished = False
        self._session_closed = False
        self._close_guard = threading.Lock()
        #: The scan consults this at every poll boundary; it is the only way this runner can
        #: hold a scan that is already running.
        session.control.pacing = self._pace

    @property
    def stats(self) -> SearchStats:
        return self.session.stats

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def waiting_for_a_reader(self) -> bool:
        """True while the pull is held at a boundary, slice in hand.

        A search that is waiting for its reader is doing no work, which is the difference
        between a search that is slow and one that has been given up on.
        """
        with self._condition:
            return self._taken < self._parked

    # --- the event loop side ---

    async def next_step(self) -> Step:
        """The next slice, and whether there will ever be more.

        A cancelled search ends the way an exhausted one does - nothing left to hand over -
        because the scan stops rather than raising. `stats.cancelled` says which of the two it
        was. A pull that died raises here, so a search that fails mid-stream is reported on the
        stream rather than as a silently short one.
        """
        if self._finished:
            return Step(pairs=[], exhausted=True)
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._start_pull(loop)
        while True:
            with self._condition:
                if self._taken < self._parked or not self._pulling:
                    step = Step(pairs=self._budget.take(), exhausted=self._exhausted)
                    # Only ever one handover per parked slice, so the loop cannot drain the
                    # pull's pacing clock by asking for slices it has already been given.
                    self._taken = self._parked
                    self._improvements_handed_over = self.stats.improvements
                    self._condition.notify_all()
                    break
                # Clear before re-checking, so a pull that parks between the check and the
                # await still gets this loop moving again.
                self._ready.clear()
                if self._taken < self._parked or not self._pulling:
                    continue
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=self._pool.patience_seconds)
            except TimeoutError:
                # A patience timeout is not an error: it is this loop looking again. A
                # cancellation is not looked at again - it propagates to the response, whose
                # `finally` cancels the search and lets the parked pull go.
                continue
        if step.exhausted:
            self._finished = True
            if self._error is not None:
                raise self._error
        # So that `GET /v1/searches` says how far this search got, right now.
        self.stats.comparison_polls = self.session.control.polls
        return step

    def cancel(self, reason: str) -> None:
        """Stop this search, and let the pull reach the scan's own stop check.

        Never blocks and never awaits, so it is safe to call from a `finally` while the request
        task is being cancelled. A pull parked at a boundary wakes here and then stops parking,
        so it stops comparing within one poll window instead of walking out the rest of the
        candidates - which is the whole difference between a laptop that goes quiet and one
        that spends four minutes on a search nobody is reading.
        """
        self.session.control.cancel(reason)
        self._finished = True
        with self._condition:
            started = self._started
            self._taken = self._parked
            self._condition.notify_all()
        if not started:
            # The pull is queued behind something else and the loop will never read it again;
            # closing here is safe because no thread has reached the generator yet, and the
            # pull - if it still runs - finds a cancelled control and does nothing.
            self._close_session()

    # --- the pull side ---

    def _pace(self) -> None:
        """Hold the scan at a poll boundary until the reader has taken what was found.

        Runs on the pull thread, inside the scan's own poll window. A slice ends when it is
        full, when its time is up, or when the best-so-far list got better - that last one is
        the whole point of watching a search, and stopping for it is what lets a watcher see
        every list the ranking produced rather than a sample of them. Never holds after a stop
        has been asked for, and never holds for a slice that is not worth stopping for.
        """
        if self.session.control.cancelled:
            return
        if self._budget.spent or self.stats.improvements != self._improvements_handed_over:
            self._park()

    def _park(self) -> None:
        """Say "here is a slice" on the event loop, and wait to have it taken.

        A cancelled pull stops waiting rather than holding: the point of being cancelled is to
        stop, and the scan's stop check is the very next thing after this boundary.
        """
        with self._condition:
            if self.session.control.cancelled:
                return
            self._parked += 1
            self._wake()
            while self._taken < self._parked and not self.session.control.cancelled:
                self._condition.wait()
            self._taken = self._parked

    def _pull(self) -> None:
        """Walk the scan from end to end, handing it over a slice at a time.

        The only thing that closes the session: whether this walk finished, was cancelled, or
        died, it is the last thing standing on the scan's thread, so the generator and the
        cache connection have exactly one owner and exactly one closing.
        """
        try:
            for match in self.session.iterator():
                self._budget.collected.append(match)
                self._pace()
        except BaseException as error:  # reported on the stream by the reader of these steps
            with self._condition:
                if self._error is None:
                    self._error = error
        # The last slice still has to reach whoever is reading, or a search loses its finds -
        # and the pull must always be marked over, or a loop waiting for a slice waits forever.
        try:
            self._park()
        except BaseException as error:
            with self._condition:
                if self._error is None:
                    self._error = error
        finally:
            with self._condition:
                self._pulling = False
                self._exhausted = True
                self._condition.notify_all()
            self._wake()
            self._close_session()

    # --- shared plumbing ---

    def _start_pull(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._condition:
            if self._started or self._pulling:
                return
            self._started = True
            try:
                loop.run_in_executor(self._pool.executor, self._pull)
            except BaseException as error:  # the pool is gone: the server is going away
                self._started = False
                self._pulling = False
                self._exhausted = True
                if self._error is None:
                    self._error = error
                self._condition.notify_all()
                self._close_session()
                return
            self._pulling = True

    def _wake(self) -> None:
        """Tell the event loop a slice is waiting. Safe from the pull thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake_on_loop)
        except RuntimeError:
            pass  # the loop is closed: nobody is reading this search any more

    def _wake_on_loop(self) -> None:
        self._ready.set()

    def _close_session(self) -> None:
        """Close the search once, whoever gets here first.

        Its own lock rather than the condition, because this is called both from the pull (which
        may be holding the condition) and from a cancel on the event loop.
        """
        with self._close_guard:
            if self._session_closed:
                return
            self._session_closed = True
        self.session.close()
