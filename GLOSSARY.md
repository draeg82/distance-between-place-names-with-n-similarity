# Place-name similarity and distance

Finding pairs of places whose ASCII names differ by exactly *n* characters, and reporting
how far apart each pair is. Everything in this repository serves that question, and the
language below is the language the code uses for it.

## Language

### Comparing names

**Character difference**:
The number of positions at which two equal-length ASCII names hold different characters.
Differences are positional: nothing is inserted or deleted, so names of different lengths
have no character difference at all.
_Avoid_: edit distance, Levenshtein distance, similarity score

**Threshold**:
The character difference being asked for. A pair matches when its character difference
equals the threshold exactly — never at most, never at least.
_Avoid_: n (in prose), tolerance, fuzziness

**ASCII name**:
A place's romanised spelling, and the only name ever compared. Accented letters are
different characters from the plain letters they fold to, and case is a difference.
_Avoid_: place name, title, label

**Display name**:
A place's name in its own script and spelling, carried so results can be shown to a person
and never used for matching.
_Avoid_: name, original name

**Alternate names**:
Other spellings a place is known by. Not carried by this tool at all, so they cannot
influence a match.
_Avoid_: synonyms, aliases

### Places and the data

**Place**:
One named settlement, with coordinates and a population. Two places may share one ASCII
name; they are still two places.
_Avoid_: city, location, record

**Dump**:
A GeoNames places file: one file per population cut, `cities1000.txt` and so on. The
number in the name is the cut the file was made at, not a promise about the rows inside it.
An input to the import, and the raw material of a parity proof: no search reads one.
_Avoid_: CSV, table, search source

**Dataset**:
How big a place has to be to take part in a search at all. Written as a threshold (`5000`),
or as the dump that threshold is published as (`cities5000`, `cities5000.txt`); either way
the search reads the place database and keeps the places with that population or more.
Leaving it out searches every place in the database - see
`docs/adr/0007-the-place-database-is-the-only-search-source.md`.
_Avoid_: input, source, default dataset, file to search

**Dataset choice**:
One thing a search can be limited to, offered because the places actually in the database
make it different from every other choice offered. Labelled in words a person can act on —
"cities of 5,000 or more people" — with the bare number beside the label, never as it.
_Avoid_: dropdown value, option, dataset list

**Whole database**:
The dataset choice that applies no rule of its own: every place in the database, whatever
its population. Reads as "every place in the database", and carries no place count, because
a label that counts moves every time the cache is imported.
_Avoid_: all, none, default dataset, 0

**Population floor**:
The least population a place may have to take part in a comparison at all. A place sitting
exactly on the floor takes part. A floor and a dump threshold are the same number and mean
the same thing, though a floor is not the same set of rows as the dump of that name - the
floor reads populations, the dump reads a file. A dataset choice is a floor too, so a search
given both runs at whichever of the two is stricter, and neither one trims the results.
_Avoid_: filter, minimum population of a result

**Candidate set**:
The places that survive the population floor. It is decided before any pair is compared, so
it changes what is compared rather than what is displayed.
_Avoid_: filtered results

**Place database**:
The single generated table of places, deduplicated by GeoNames id, that replaces choosing
between dumps. Built from the dumps by an import command, git-ignored, and throwaway. The
only thing a search reads: a search that cannot open it says so instead of searching
something else.
_Avoid_: cache, index, data file

**Source threshold**:
On a place: the smallest population threshold whose dump contained it. It records which
dumps were available to the import, not which dumps exist - importing a subset records the
smallest of those. Provenance only: a dataset choice filters populations, so a place imported
from a wider dump still answers a narrower dataset.
_Avoid_: origin, provenance, imported from, dataset

**Name-length group**:
The candidates whose ASCII names hold one number of characters. Matching needs equal lengths,
so a group is the largest set of places that can be compared with each other, and the unit a
search walks one at a time.
_Avoid_: bucket (in prose), partition, hash

**Search scope**:
What a search is about to do, stated before it does it: how many places, at what effective
floor, in how many name-length groups, holding how many pairs.
_Avoid_: estimate, preview, metadata

### Searching

**Pair**:
Two places considered together, reported once. A place counts as a pair with itself, which
at a threshold of zero is a character difference of zero at a distance of nothing. Which
half is named first follows the order the candidates were walked in, so the same pair can be
reported in either direction by two different paths - see
`docs/adr/0005-pair-order-follows-candidate-order.md`.
_Avoid_: match (as a noun for the two places), combination, duplicate

**Scan**:
One complete walk over a candidate set at one threshold. A scan is a stream: pairs arrive as
they are found rather than at the end.
_Avoid_: query, run, job

**Result**:
The pairs a scan reported, in the order the candidates were walked: name-length group then
increasing population for a search of the place database. A dump's file order survives only
where a parity proof reads a dump, which is not a search.
_Avoid_: output, answer

**Great-circle distance**:
How far apart two places are along the sphere, in kilometres — the distance a straight line
drawn on a globe would measure.
_Avoid_: haversine distance (in prose), driving distance, displacement

**Everywhere mode**:
A search that names no place to start from: everything in the chosen dataset is compared with
everything else at one character difference, and the answer is the pairs that are furthest, or
nearest, wherever they are. The question the tool exists for.
_Avoid_: global search, all-pairs mode, exhaustive scan

### Watching a search

**Search session**:
One search as an object: the request it was made from, the candidate set the request resolved
to, the pairs it reports as it finds them, and the counters that say how far it got. The
terminal and the stream both drive a session and nothing else.
_Avoid_: job, task, query handle

**Search stream**:
A search written out as newline-delimited JSON while it runs - one `started`, `progress` and
`results` as it goes, one `done` at the end. The events are the search, not a report of it
afterwards.
_Avoid_: SSE, websocket, polling

**Best-so-far list**:
The best distances found so far, at most `top_k` of them, in the order the core ranked them.
Every `results` event carries that list at a moment when the scan is still going, which is
what makes a search watchable.
_Avoid_: partial results (as a name), leaderboard, answer

**Coalescing**:
Sending the best-so-far list at most once per interval, however many times the ranking
improved inside it. A hot scan cannot flood a client.
_Avoid_: batching, debouncing, buffering

**Slice**:
How much of a scan is pulled aside before the scan is let go: the finds so far, plus as much
looking as `--step-seconds` allows, and stopping at once when the best-so-far list improves.
_Avoid_: batch, chunk, page

**Pacing**:
Holding a scan at one of its own poll boundaries until the reader watching it has taken what
has been found, so the scan goes at the reader's speed rather than the machine's - see
`docs/adr/0006-a-watched-scan-is-held-at-its-own-poll-boundaries.md`.
_Avoid_: throttling, backpressure (in prose), rate limiting

**Watching**:
Reading a search's stream while it happens, from a terminal or a browser, and stopping it by
hanging up.
_Avoid_: tailing, following, live view

### On the map

**Leading find**:
The pair at the head of the best-so-far list, which the map draws bold and the other finds sit
faint behind. The list is already ranked when it arrives, so the lead is the API's decision, not
the page's.
_Avoid_: top result, winner, best match

**Focused pair**:
The find a reader has picked out of the list: the one whose arc is bold and whose two coordinates
are printed under the map. Choosing it changes what is emphasised and nothing else - the order of
the list is untouched.
_Avoid_: selected item, active row, highlighted pair

**World window**:
The one map the page draws: longitudes folded into it, so an arc that leaves the right edge is
picked up at the left at the same height. Two pieces of one arc, never a line streaking across a
whole world.
_Avoid_: viewport, pan, wrap-around hack

**Coarse coastline**:
The committed land outline the map is drawn on - outer rings, land inside a ring, one tenth of a
degree of a map. Coarse on purpose: it says continent, not shore, so a coastal town may be drawn in
the water (`docs/adr/0009-the-map-is-drawn-from-a-committed-coastline.md`).
_Avoid_: basemap, tiles, map data

### The shape of the tool

**The seam**:
Matching, distance, place reading, scanning, and the candidate generation that decides what
gets compared: the part with the rules in it, and the part everything is tested through.
_Avoid_: library, business logic, engine

**Adapter**:
A way in — the terminal, the search API, the browser — that presents the seam's answers and
holds no matching rules of its own.
_Avoid_: client, frontend, entry point
