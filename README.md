# DistNSimilarity

Find pairs of places whose ASCII names differ by exactly *n* characters, and how far
apart they are. Input is a [GeoNames](https://download.geonames.org/export/dump/) places
dump; the committed dumps are `cities1000.txt`, `cities5000.txt` and `cities15000.txt`.
The number in a dump's name is the population it was cut at, not a guarantee: the
committed dumps still contain places below it (677 in `cities5000.txt`, 52 in
`cities15000.txt`), so a population floor is something this tool applies rather than
inherits.

## Install

One step, using [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

That creates `.venv` in this directory with its own interpreter and installs everything
in `pyproject.toml`. Nothing here depends on which Python wins the `PATH` search: every
command below runs through `uv run`, which uses `.venv`.

## Run a search from the terminal

```bash
uv run distnsimilarity-search --dataset 15000 --n 1 --output /tmp/out.csv
```

| Flag | Meaning |
| --- | --- |
| `--dataset` | Population threshold (`15000`), file name (`cities15000.txt`) or a path to a dump. Required. |
| `--n` | Character-difference value: pairs match when their equal-length ASCII names differ in exactly this many positions. Required. |
| `--output` | Where to write the CSV. Required. |
| `--min-population` | Optional population floor; places below it are never compared. |
| `--full-comparison` | Compare every place against every later place instead of only places whose ASCII name is the same length. Same pairs, same order, same CSV; far slower. |

`--dataset`, `--n` and `--output` are always supplied by the caller: the tool has no
default input file and no default output location. A dataset request that cannot be
resolved fails with the request and the list of available datasets.

Output is UTF-8 with `\r\n` record separators, header
`place1,lat1,lon1,place2,lat2,lon2,haversinedistance`, distances in kilometres.

Two searches over the same dumps produce the same rows in the same order.

A search only ever compares names of equal length, because the matching rule needs equal
lengths anyway - on `cities1000.txt` that is 927 million comparisons instead of 10.77
billion. `--full-comparison` switches the shortcut off; it exists so the shortcut can be
argued with, and it writes the same bytes. `scripts/benchmark_scan.py` measures it:

```bash
uv run python scripts/benchmark_scan.py --dataset 1000 --n 1 --sample 2000 --database
```

With `--database` it also builds a temporary place database and times the database-backed
walk against the in-memory one at the same population floor - same pairs, and the indexed
lookup costs nothing measurable per comparison.

## Build the place database

The three dumps contain each other - every GeoNames id in `cities5000.txt` is also in
`cities1000.txt`, and every id in `cities15000.txt` is in both - so searching them means
choosing between three copies of overlapping places. One generated table replaces them:

```bash
uv run distnsimilarity-import
```

```
146782 place(s) in places.sqlite3 from cities1000.txt, cities5000.txt, cities15000.txt:
230160 row(s) read, 83378 duplicate row(s) ignored, 0 row(s) refused
```

230,160 rows in, 146,782 places out, in about a second. The database is generated and
git-ignored, never committed: delete it and build it again whenever you like.

| Flag | Meaning |
| --- | --- |
| `--database` | Where the SQLite file goes. Default: `places.sqlite3` beside the dumps - never a home directory. |
| `--dataset` | Dump to import: population threshold, file name or path. Repeatable. Default: every dump in the data directory. |
| `--recreate` | Drop the table and rebuild it from the dumps given. |

One row per GeoNames id, in a table carrying the ASCII name, display name, coordinates,
population, country code, name length, and `source_threshold`: the smallest population
threshold whose dump contained that place. Re-running the import is idempotent - the second
run adds nothing and changes nothing. A row whose coordinates are off the globe, or that is
missing a column, is reported on stderr with its file and line number and the command exits
non-zero; nothing is dropped quietly.

### Searching the database

Candidate generation is a lookup rather than a comparison: a name-length group at a
population floor is one indexed query, and the pairs come out of the same inner loop a
dump scan uses.

```python
from distnsimilarity.db import connect, scan_database, scope_for

database = connect("places.sqlite3")
print(scope_for(database, 5_000).summary())
for pair in scan_database(database, character_difference=1, population_floor=5_000):
    print(pair.first.asciiname, pair.second.asciiname, pair.distance_km)
```

`scope_for` answers "what is about to be searched" - place count, effective floor, the
name-length groups and the pairs they hold - so an interface can say the cost before paying
for it. Measured on the merged table:

| Floor | Places | Pairs compared | A full comparison would consider |
| --- | --- | --- | --- |
| 0 | 146,782 | 927,424,318 | 10,772,551,153 |
| 1,000 | 124,272 | 666,215,784 | 7,721,827,128 |
| 5,000 | 55,108 | 139,850,045 | 1,518,473,386 |
| 15,000 | 27,666 | 37,306,573 | 382,717,611 |
| 100,000 | 5,072 | 1,420,897 | 12,865,128 |

The floor narrows the *candidate set*, so a higher floor makes the search shorter rather than
the report shorter - see `docs/adr/0004-floor-filters-candidates-not-results.md`. It is
inclusive: a place of exactly 5,000 is a candidate at a floor of 5,000. And it is computed
from populations, not from which file a place arrived in, which is why a floor of 5,000
selects 55,108 places while `cities5000.txt` holds 55,700 rows.

Three indexes serve the two search modes: `(name_length, population, geonameid)` to walk a
name-length group, `(population)` for the counts above, and `(asciiname)` to find a place by
name. `tests/test_place_database.py` fails if any of those lookups degenerates into a table
scan, on a fixture and on the real 146,782 places.

The terminal still reads dumps, so the CSV it writes stays exactly what the original tool
wrote. The search API and the browser search the database.

## Watch a search over HTTP

The same search, streamed. Start the server and read a search as it happens:

```bash
uv run distnsimilarity-serve --data-dir .      # 127.0.0.1:8000
uv run distnsimilarity-watch --dataset 15000 --n 1
```

`GET /v1/search` answers newline-delimited JSON, one event per line, while the scan runs:

| Event | Carries |
| --- | --- |
| `started` | the request back, what it resolved to, `candidate_pairs`, and the cost estimate |
| `progress` | pairs compared, share of the candidate pairs, elapsed and remaining time |
| `results` | the current best-so-far list: both places, the distance, and how many positions the names differ in |
| `done` | final counts, elapsed total, and whether the search completed, was cancelled (with a reason, and `retryable: true`) or failed |

Other endpoints: `/healthz` (alive, and how many searches are live), `/v1/datasets` (what can
be searched, and the state of the place cache), `/v1/estimate` (what a search would cost,
without searching), and `/v1/searches` plus `/v1/searches/{id}` (what is running now, and the
last few that ended).

Three things about the stream are deliberate:

- **The scan waits for its reader.** A search is pulled a slice at a time - as many finds as
  `--batch-size`, or `--step-seconds` of looking, or until the best-so-far list improves -
  which is why a list arrives *during* the scan rather than after it, and why a rare match
  does not arrive in the same event as the whole rest of the file.
- **Improvements are coalesced.** The ranking may change twenty times in `--results-interval`
  (0.25 s by default) and the client is sent the current list once. Progress ticks on their
  own, on `--progress-interval`, and only when something has actually been compared.
- **One search at a time.** A second `GET /v1/search` while one runs is refused with 409
  naming the search in the way and how to stop it; a queued second scan would only make the
  first slower and hide that. Hanging up - Ctrl-C in the watcher - cancels the scan within a
  few thousand comparisons rather than letting it run to the end for nobody.

The watcher prints the cost first, then the best list again every time the server sends a
better one; `--rows` trims it and `--no-progress` leaves the ticks out. It ranks, sorts and
filters nothing: the order it prints is the order the `results` events arrived in.

## Run the tests

```bash
uv run pytest
```

That is the whole default suite: no network access, no writes outside a temporary
directory, and none of the committed dumps loaded - 350-odd tests in under a second.
Tests that do read real data are marked `slow` and excluded; run them with
`uv run pytest -m slow`. The slow suite imports all three dumps into a temporary database,
checks the 230,160 → 146,782 deduplication against the real files, and confirms the
query plans on a table big enough for the planner to mean it; it takes about four seconds.

## Using the core directly

`distnsimilarity.core` is the seam everything else - the command line, the API, the
browser - is an adapter over. It is importable and callable on its own, with no server and
no browser:

```python
from pathlib import Path

from distnsimilarity.core.matching import differing_positions, matches_character_difference
from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.places import read_places
from distnsimilarity.core.scan import scan

assert differing_positions("Genoa", "Genoa") == 0
assert matches_character_difference("Lima", "Loma", 1) is True
assert matches_character_difference("Aba", "Abba", 1) is False  # unequal lengths never match
assert great_circle_distance_km(0.0, 0.0, 1.0, 0.0) == 111.1950802335329  # one degree of arc

for match in scan(read_places(Path("cities15000.txt")), 1):
    print(match.first.asciiname, match.second.asciiname, match.distance_km)
```

The rules are the ones the original script implemented, and they are frozen by
`tests/test_matching_rules.py`, `tests/test_distance_rules.py` and
`tests/test_scan_reports.py`: equal lengths are required, differences are counted
positionally rather than as edits, a pair matches at exactly the requested count and no
other, comparison is case-sensitive, accents count as differing from their folded letters,
and only the ASCII name column is compared.

## Troubleshooting

### If the tool will not start

`ModuleNotFoundError: No module named 'distnsimilarity'` from a console script means the
editable install is not registered. Registration happens through a `.pth` file in the
virtual environment's `site-packages`, and CPython's `site` module **silently skips a
`.pth` file carrying the macOS `hidden` file flag**:

```bash
find .venv -name "*.pth" -flags +hidden   # any output is the problem
```

Confirmed: that flag is what breaks the import — clearing it (`chflags nohidden <file>`)
fixes it, and the same `uv sync` produces no flagged files outside `~/Desktop` and
`~/Documents`. Suspected cause: the cloud-sync agent for those two macOS folders sets the
flag on files installers write there; flagged files have been seen to reappear while
nothing was installing. Which process does it is not established.

Either remedy is environment-only; neither changes this repository:

```bash
# put the environment outside the synced folders, and export the same variable for `uv run`
export UV_PROJECT_ENVIRONMENT="$HOME/venvs/distnsimilarity"
uv sync --extra dev
```

...or keep the clone somewhere other than `~/Desktop` / `~/Documents`.

## Datasets

The dumps are read from the repository root. `DISTNSIMILARITY_DATA_DIR` overrides that
location; nothing is ever resolved against a home directory. The generated
`places.sqlite3` lives beside the dumps unless `--database` says otherwise, and is ignored
by git.

## Portability

Declared runtime dependencies carry version floors (`pyproject.toml`), including
`haversine` — whose two-position tuple call signature is relied on — and `uvicorn`, the
ASGI server behind the search API. Encoding and line endings are explicit on every file
the tool writes. `tests/test_portability.py` keeps this honest: it fails if a personal
absolute path reappears in the source, and it runs the tool from an unrelated working
directory.
