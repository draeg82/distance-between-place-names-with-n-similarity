"""What the server knows about the searches it is running, and has run recently.

The registry exists so that "is that search still running?" is a question with an answer
over HTTP: a client that gave up mid-stream can ask, and a test can check that nothing is
running after a cancellation instead of hoping. It is not a result cache - completed
searches keep their statistics and their search id, not their pairs.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from distnsimilarity.search import MeasuredRate, SearchSession, SearchStats


def new_search_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class SearchRecord:
    """One search the server opened, and how much of it is still going."""

    search_id: str
    request: dict[str, object]
    started_at: float
    stats: SearchStats
    live: bool = True
    finished_at: float | None = None

    @property
    def seconds(self) -> float:
        end = time.time() if self.finished_at is None else self.finished_at
        return round(end - self.started_at, 3)

    # How a search ended is the search's own answer, not something the registry keeps a second
    # copy of: these read the statistics the scan writes, so a caller can ask the record without
    # reaching past it into the running search.

    @property
    def completed(self) -> bool:
        """Did this search walk all of its candidates?"""
        return self.stats.completed

    @property
    def cancelled(self) -> bool:
        """Was this search told to stop?"""
        return self.stats.cancelled

    @property
    def cancel_reason(self) -> str:
        """Why this search was told to stop, or empty if nothing did."""
        return self.stats.cancel_reason

    def as_dict(self) -> dict[str, object]:
        return {
            "search_id": self.search_id,
            "live": self.live,
            "request": dict(self.request),
            "seconds": self.seconds,
            **self.stats.as_dict(),
        }


@dataclass
class SearchRegistry:
    """Live searches plus the last few that ended, guarded by one lock.

    `recent` is bounded, because a server that remembers every search forever is a memory
    leak with a statistics endpoint bolted on.

    It also holds `rate`, the last scan rate this process measured, because that is the only
    basis a cost estimate has - and `one_at_a_time`, because a second scan on a laptop is a
    slower first scan, and a queue would hide that instead of saying it.
    """

    keep: int = 200
    one_at_a_time: bool = True
    rate: MeasuredRate = field(default_factory=MeasuredRate, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _live: dict[str, SearchRecord] = field(default_factory=dict, repr=False)
    _recent: Deque[SearchRecord] = field(default_factory=deque, repr=False)

    def claim(
        self, session: SearchSession, request: dict[str, object]
    ) -> tuple[SearchRecord | None, SearchRecord | None]:
        """Take the server for a search, or find out who has it.

        Returns `(record, None)` when the search may run, and `(None, running)` when another
        one is in the way. Both halves are decided under one lock, so two simultaneous
        requests cannot both be told they are the only search.
        """
        with self._lock:
            if self.one_at_a_time and self._live:
                return None, next(iter(self._live.values()))
            record = SearchRecord(
                search_id=new_search_id(),
                request=dict(request),
                started_at=time.time(),
                stats=session.stats,
            )
            self._live[record.search_id] = record
            return record, None

    def finish(self, record: SearchRecord) -> None:
        """A search has ended: stop counting it, and let it price the next one.

        The rate is recorded here rather than during the scan because only a search that ran to
        the end is a measurement of what searching costs. A scan that was stopped part way
        spent part of its time being opened and handed back, and quoting it would make every
        later estimate too optimistic.
        """
        with self._lock:
            self._live.pop(record.search_id, None)
            record.live = False
            record.finished_at = time.time()
            self._recent.append(record)
            while len(self._recent) > self.keep:
                self._recent.popleft()
        if record.stats.rate_per_second() is not None:
            self.rate.record(record.stats.pairs_compared, record.stats.elapsed_seconds())

    def get(self, search_id: str) -> SearchRecord | None:
        with self._lock:
            for record in list(self._live.values()) + list(self._recent):
                if record.search_id == search_id:
                    return record
        return None

    def live(self) -> list[SearchRecord]:
        with self._lock:
            return list(self._live.values())

    def recent(self) -> list[SearchRecord]:
        with self._lock:
            return list(self._recent)

    @property
    def live_count(self) -> int:
        with self._lock:
            return len(self._live)
