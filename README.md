# DistNSimilarity

Find pairs of places whose ASCII names differ by exactly *n* characters, and how far
apart they are. Every search reads one **place database**, generated from a
[GeoNames](https://download.geonames.org/export/dump/) places dump; the dumps themselves
- `cities1000.txt`, `cities5000.txt` and `cities15000.txt` - are the input to that import,
not something a search reads.
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

Once, before the first search: build the place database from the dumps (see
[Build the place database](#build-the-place-database)). Then:

```bash
uv run distnsimilarity-search --dataset 15000 --n 1 --output /tmp/out.csv
```

| Flag | Meaning |
| --- | --- |
| `--dataset` | Optional dataset: a population threshold (`15000`) or the dump it is published as (`cities15000.txt`), both meaning "cities of 15,000 or more people". Omit it to search every place in the database. |
| `--n` | Character-difference value: pairs match when their equal-length ASCII names differ in exactly this many positions. Required. |
| `--output` | Where to write the CSV. Required. |
| `--min-population` | Optional population floor; places below it are never compared. |
| `--database` | Which place database to search. Default: `$DISTNSIMILARITY_DB`, else `places.sqlite3` beside the dumps. |
| `--full-comparison` | Compare every place against every later place instead of only places whose ASCII name is the same length. Same pairs, same rows, far slower; only the order a plain listing arrives in differs. |

`--n` and `--output` are always supplied by the caller: the tool has no default output
location. The database does have a default, because there is one thing to search: a search
over a database that has not been imported fails with the `distnsimilarity-import` command
to run, rather than reading a dump instead. A dataset that names no rule fails with the
request and the datasets this database can be searched as; a dataset bigger than every
place in it fails the same way, because "no pairs" would look like an answer.

Output is UTF-8 with `\r\n` record separators, header
`place1,lat1,lon1,place2,lat2,lon2,haversinedistance`, distances in kilometres.

Two searches over the same place database produce the same rows in the same order.

A search only ever compares names of equal length, because the matching rule needs equal
lengths anyway - on `cities1000.txt` that is 927 million comparisons instead of 10.77
billion. `--full-comparison` switches the shortcut off; it exists so the shortcut can be
argued with, and it finds the same pairs and writes the same rows. The shortcut walks one
name length at a time, so a plain listing arrives in a different order with the shortcut
off - a top-K run is ordered by distance either way. `scripts/benchmark_scan.py` measures it:

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

### Searching the place database

Candidate generation is a lookup rather than a comparison: a name-length group at a
population floor is one indexed query, and the pairs come out of the same inner loop the
original script used. A dataset is a floor too, so `dataset_threshold` and
`population_floor` are folded into whichever of the two is stricter.

```python
from distnsimilarity.db import connect, scan_database, scope_for

database = connect("places.sqlite3")
print(scope_for(database, dataset_threshold=5_000).summary())
for pair in scan_database(database, character_difference=1, dataset_threshold=5_000):
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

The terminal, the API and the browser all search this table - see
`docs/adr/0007-the-place-database-is-the-only-search-source.md`. Dumps are still read where
a proof needs the file itself: `distnsimilarity-import`, `scripts/benchmark_scan.py`, the
`slow` tests, and the byte-for-byte CSV parity pinned by
`docs/adr/0005-pair-order-follows-candidate-order.md`.

## Watch a search over HTTP

The same search, streamed. Start the server and read a search as it happens:

```bash
uv run distnsimilarity-serve --data-dir .      # 127.0.0.1:8000
uv run distnsimilarity-watch --dataset 5000 --n 1
```

`--dataset` is optional here too: left out, the watcher searches every place in the
server's database.

`GET /v1/search` answers newline-delimited JSON, one event per line, while the scan runs:

| Event | Carries |
| --- | --- |
| `started` | the request back, what it resolved to, `candidate_pairs`, and the cost estimate |
| `progress` | pairs compared, share of the candidate pairs, elapsed and remaining time |
| `results` | the current best-so-far list: both places, the distance, and how many positions the names differ in |
| `done` | final counts, elapsed total, and whether the search completed, was cancelled (with a reason, and `retryable: true`) or failed |

Other endpoints: `/healthz` (alive, how many searches are live, and the state of the place
database), `/v1/datasets` (what a search can be limited to, in words, and what each choice
would cost), `/v1/estimate` (what a search would cost, without searching), and
`/v1/searches` plus `/v1/searches/{id}` (what is running now, and the last few that ended).

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

### Or watch it in a browser

The same server serves the app, so there is still one process and one port:

```bash
uv run distnsimilarity-serve --data-dir .      # 127.0.0.1:8000, and open that address
```

The form is everywhere mode: a character-difference value, a dataset, an optional population
floor, a direction and a list size - the same parameters the watcher takes, with the dataset
choices and their wording read from `/v1/datasets` rather than written into the page. The price
of a search is asked of `/v1/estimate` as those choices are made, so it is on screen before the
stream opens rather than after it: the sentence names the floor and the character difference it
was priced at, and a parameter string that already has a price is not counted twice. A search can
be stopped while it runs, which hangs up on the stream - the only way this API is told to cancel -
and the list on screen stays as what was found so far, with a line saying nothing more is coming.
One search runs per page and one scan per machine, so a second Search while one is running is
refused rather than queued or restarted: the refusal says which search is running, how far it has
got, and that Stop is the way out. Changing the parameters and searching again is the same page -
nothing reloads, so the list, the map and the price are still there to compare the next answer
with. The working indicator and the progress line say what the scan is doing while it does it; the
best-so-far list is replaced whole every time the server sends a better one, in place, without a
refresh. A search that matches nothing says so with the parameters it used, because "nothing over
every place in the database" and "nothing over towns of 5,000" are different findings.

The map underneath the form draws the same list. The leading find is the bold arc and every other
find is faint behind it; the arcs come from the latitudes and longitudes the `results` events
already carry, and they draw themselves in as the list improves, on frames rather than on a timer.
Pressing a row - or tabbing to it and pressing Enter - makes that pair the bold one and prints both
places' coordinates under the map. A search that matches nothing clears the map rather than leaving
the previous search's pair on it, and a person who has asked their pages not to move gets the arcs
already drawn.

The front end is six files served from the package - a page, three ES modules, a stylesheet and a
coastline - with no build step and nothing loaded from another machine
(`docs/adr/0008-the-front-end-is-plain-es-modules.md`). `results.js` turns events into words and
table cells; `map.js` turns coordinates into a curve; `app.js` asks for a search, reads the stream
a line at a time and draws what comes back. None of them matches, ranks, sorts or measures: the
order on screen is the order the `results` events arrived in, the highlighted characters are the
positions the API said differ, and the kilometres beside a pair are the API's - the map bends an
arc between two places it was given and never works out how far apart they are.

The map is a canvas and a JSON coastline, not a mapping library
(`docs/adr/0009-the-map-is-drawn-from-a-committed-coastline.md`). The outline is Natural Earth 110m
land, simplified to about 2,400 vertices and 32 KB, committed in the package and rebuilt by
`scripts/make_world_outline.py` whenever somebody wants it finer. It is coarse enough that a
coastal town can sit in the water, which is the right trade for a picture whose job is to show
where an arc goes.

## Run the tests

```bash
uv run pytest
```

That is the whole default suite: no network access, no writes outside a temporary
directory, and none of the committed dumps loaded - 600-odd tests in under a second.
Tests that do read real data are marked `slow` and excluded; run them with
`uv run pytest -m slow`. The slow suite imports all three dumps into a temporary database,
checks the 230,160 → 146,782 deduplication against the real files, and confirms the
query plans on a table big enough for the planner to mean it; it takes about four seconds.

The front end's presentation is checked by running its module with Node, which turns
"the differing characters are highlighted" into an assertion about output rather than a
look at a screenshot. The map is checked the same way: its arcs are sampled, and its painter is
handed a canvas context that writes down what it was asked to do, so "a place at 90 east is drawn
three quarters of the way across" and "an arc that crosses the antimeridian arrives at the far side
at the same height" are assertions about numbers. The coastline is checked as data, by asking
whether known cities and a known patch of ocean are on the right side of it. Node is a developer's
tool and not a dependency: where it is not installed those few tests skip and say so, and
everything else still runs.

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

`read_places` is the reader the importer and the parity proofs use; a search of the product
reads the place database and hands the same `Place` objects to the same `scan`.

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

A dataset is how big a place has to be to take part in a search, and it is spelled either as
a threshold (`5000`) or as the dump that threshold is published as (`cities5000`,
`cities5000.txt`). Searching a dataset never reads that dump: it reads the place database and
keeps the places at that population or above. So a floor of 5,000 selects the 55,108 places
in the table with 5,000 or more people, which is close to `cities5000.txt` but not the same
rows - the rule is about people, not files.

`/v1/datasets` lists the choices this database can actually offer, worded ("cities of 5,000
or more people", thousands separated), each with its place count and comparison count, plus
`every place in the database` for a search that applies no rule. A choice that would search
exactly the places another choice searches is not offered, so a database imported from
`cities1000.txt` alone offers two choices rather than four impossible ones.

The dumps are read from the repository root by the import command.
`DISTNSIMILARITY_DATA_DIR` overrides that location; nothing is ever resolved against a home
directory. The generated `places.sqlite3` lives beside the dumps unless `--database` says
otherwise, `DISTNSIMILARITY_DB` overrides where a *search* looks for it, and both files are
ignored by git.

## Portability

Declared runtime dependencies carry version floors (`pyproject.toml`), including
`haversine` — whose two-position tuple call signature is relied on — and `uvicorn`, the
ASGI server behind the search API. Encoding and line endings are explicit on every file
the tool writes. `tests/test_portability.py` keeps this honest: it fails if a personal
absolute path reappears in the source, and it runs the tool from an unrelated working
directory.
