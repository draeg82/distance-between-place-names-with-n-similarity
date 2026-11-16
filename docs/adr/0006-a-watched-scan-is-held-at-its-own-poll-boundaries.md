# A watched scan is held at its own poll boundaries

A search has to answer with pairs, progress and a cost while the scan is still running. On the
data this tool exists for that is hard in a specific way: matches are rare. A million-name dump
may hold one pair at n=1, found in the first few thousand comparisons and then nothing for
minutes. If the scan runs flat out on a worker thread and writes into a queue, the queue drains
into the socket in one go, `progress` says 100%, and "partial results" describes the one moment
the scan happened to finish. Cancelling the scan and starting it again per event is not better:
a scan cannot be restarted part way through a name-length group without either redoing work or
remembering a cursor into it.

So the question is where a scan can be interrupted at all. Its inner loop compares billions of
names and takes no lock; the one place it is already standing still, having counted what it has
done, is its poll boundary - every `POLL_EVERY_COMPARISONS` comparisons, where it looks at
whether it should stop.

## Decision

A scan is pulled, not pushed. `distnsimilarity.api.runner` runs a search session's pair iterator
on a worker thread and hands the event loop one **slice** at a time; between slices the scan is
parked. The mechanism is the core's `Progress` callback, which is allowed to block for exactly
this reason: it is called at a poll boundary, and blocking there holds up the scan and nothing
else. The runner's callback parks the pull until the event loop has taken what the scan has
found, so the scan goes at its reader's speed - **pacing**.

A slice ends for one of three reasons, and the third is the one that makes a stream worth
watching:

- the slice has produced `--batch-size` pairs;
- it has spent `--step-seconds` looking, which bounds how long a pair waits behind a stretch of
  scan that finds nothing;
- or the best-so-far list got better, because a new best list is the thing the reader is
  watching, and stopping for it is what lets a client see every list the ranking produced
  instead of a sample of them.

Cancellation is the same seam in reverse. A reader that goes away cancels the session; the pull
is never held after that, and the scan returns within its current poll window.

## Consequences

The scan's rate is now a property of the reader as well as the machine: a watcher that stops
reading slows the scan down, and `comparisons_per_second` measures the paced scan. That is the
honest number, and it is why a cost estimate quotes a rate measured on this machine rather than
a constant.

One worker thread is occupied by a parked pull, which is why the server has more workers than it
runs scans: opening a dataset and pricing a search still have somewhere to run while a scan
holds one thread. `--search-workers` says it out loud.

The scan's inner loop pays for a modulo per comparison only when it was given a `cancel` or
`progress` callback; a headless scan keeps its old shape, and the pinned rates in the benchmark
script are still headless ones.

Blocking in `Progress` is now part of the core's contract, so the docstring says it may block and
that the callback must be the caller's own pause and never a lock somebody else is expected to
hand back. A callback that blocks forever holds a scan and its worker forever; the runner is the
only caller that does that, and it parks only while a search is live.

What this buys is testable at both ends: `tests/test_search_seam.py` proves a held search makes
no further comparisons until its driver lets it go, and `tests/test_http_search.py` proves the
same over a socket - that a `results` event reaches a client while the server's own registry
still lists the search as live, and that a client which stops reading costs the scan the rest of
its comparisons.
