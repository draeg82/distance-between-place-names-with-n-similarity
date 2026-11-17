# The place database is the only search source, and a dataset is a population rule

The place database of ADR 0003 was built so that searches stopped re-reading three nested
copies of the same places, and it did not happen: every entry point still defaulted to a
dump, and the database was something a caller opted into with `--database` or
`source=database`. Worse, a search that did choose the database ignored the dataset it had
been given — `source=database&dataset=15000` scoped candidates by `min_population` only and
quietly searched all 146,782 places. Two sources meant every interface had to explain a
distinction nobody in front of a search cares about: which file a place arrived in.

There is now one search source: the place database. `source` is gone from `/v1/search` and
`/v1/estimate`, and a request that still sends it is refused rather than quietly honoured —
with 410 and `error: RemovedParameter` rather than a 422 about a malformed value, because
the parameter was fine and this server stopped serving it. A client speaking the older API
gets a refusal it can tell apart from its own typo, and the message says what to send
instead. A database that is missing, or present but holding no places, is the one startup
failure, and it names the `distnsimilarity-import` command that fixes it. Nothing falls back
to reading a dump: a search that silently reads a different set of places is worse than one
that refuses, and `/healthz` says `imported: false` before anybody asks.

A **dataset is a rule about population**, not a record of provenance. `dataset=5000` keeps
the places with 5,000 or more people, which is the rule a population floor applies, so a
request carrying both a dataset and a `min_population` runs at whichever of the two is
stricter and neither one trims results (ADR 0004 measured what the two spellings select;
this chooses between them). It is deliberately *not* `source_threshold <= 5000`.
That column says which dumps an import run had available — importing `cities1000.txt` alone
records every place at 1000, importing `cities5000.txt` and `cities15000.txt` records every
place at 5000 — so a dropdown built on it would be a list of what happened to be on disk,
and a city of 900,000 people could be missing from "cities of 15,000 or more people"
because of which dump wrote its row. Measured on the merged table, cities of 5,000 or more
people are 55,108 places; `cities5000.txt` holds 55,700 rows, 677 of them below 5,000. The
two are close because the same GeoNames rule cut both, and the parity claim is about
populations rather than files.

A choice is offered in words — "cities of 5,000 or more people", thousands separated — with
the bare number a field beside the label and never the label itself, and a search over
everything reads as "every place in the database". It does not read "all 146,782 places in
the database": a label that counts moves every time somebody re-imports, and then two
screens disagree about the same cache. The choices are built by counting the places behind
each threshold the tool names and dropping a threshold that would search exactly the places
a choice already on the list searches — a threshold every place in the table clears is not a
choice, it is the same search offered twice. That is decidable by counting, because a bigger
threshold only ever takes places away, so two thresholds holding the same number of places
hold the same places. A dataset no place answers is a refusal naming the choices that do, so
"this dataset does not exist here" and "this dataset has no pairs" stay different sentences.

## Consequences

`source_threshold` stays a column the importer writes and nothing filters on. It needs no
index and no place in a query plan, because no search asks it; it answers "which dumps were
available to this import", which is a question for an import log and for ADR 0003.

Both filters are `population >= ?` on one column, so folding a dataset into the floor leaves
the index seek on `(name_length, population, geonameid)` exactly as ADR 0003 measured it. A
query-plan test says so rather than assuming it, and a threshold that changed the plan would
fail that test rather than slow the search quietly.

`started`, `done` and the `/v1/searches` record carry the threshold, its words, the effective
floor and the database path, so two searches of different subsets are distinguishable after
they end and no event has to guess a filename. `requested_dataset` keeps the caller's own
spelling, so a refusal can quote back what was sent.

A row that will not make a place is an error in the stream, naming the GeoNames id, and not
a 4xx: the rows are read while the scan runs, so the search has already opened by the time
the bad row is reached. Coordinates are checked as they are read rather than when a pair is
measured, because the distance function notices a bad number two places too late to say
which row it came from.

The default database path is resolved by one function used by the command line, the server
and the watcher, with `DISTNSIMILARITY_DB` overriding it. Three defaults would let one
request report distances from one cache in one window and a different cache in another.

Pair order remains a property of the walk, not a promise (ADR 0005). With the equal-length
shortcut on, the walk goes one name-length group at a time; with `--full-comparison` there
are no groups to go by, so a plain listing arrives in a different order while holding the
same rows. A top-K run is ordered by distance either way.

Dumps are still read where a proof needs the file: `distnsimilarity-import`,
`scripts/benchmark_scan.py`, the `slow` tests over the committed dumps, and the byte-for-byte
CSV parity of ADR 0005. Outside the importer, `read_places` survives only there — it is not a
search source any more, and no product path calls it.
