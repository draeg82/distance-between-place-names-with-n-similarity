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
_Avoid_: CSV, table

**Dataset**:
The dump a particular search is run over, named by the caller — by threshold, by file name,
or by path.
_Avoid_: input, source, default dataset

**Population floor**:
The least population a place may have to take part in a comparison at all. A place sitting
exactly on the floor takes part.
_Avoid_: filter, minimum population of a result

**Candidate set**:
The places that survive the population floor. It is decided before any pair is compared, so
it changes what is compared rather than what is displayed.
_Avoid_: filtered results

### Searching

**Pair**:
Two places considered together, reported once. A place counts as a pair with itself, which
at a threshold of zero is a character difference of zero at a distance of nothing.
_Avoid_: match (as a noun for the two places), combination, duplicate

**Scan**:
One complete walk over a candidate set at one threshold. A scan is a stream: pairs arrive as
they are found rather than at the end.
_Avoid_: query, run, job

**Result**:
The pairs a scan reported, in the order the places appear in the dump.
_Avoid_: output, answer

**Great-circle distance**:
How far apart two places are along the sphere, in kilometres — the distance a straight line
drawn on a globe would measure.
_Avoid_: haversine distance (in prose), driving distance, displacement

### The shape of the tool

**The seam**:
Matching, distance, place reading and scanning: the part with the rules in it, and the part
everything is tested through.
_Avoid_: library, business logic, engine

**Adapter**:
A way in — the terminal, the search API, the browser — that presents the seam's answers and
holds no matching rules of its own.
_Avoid_: client, frontend, entry point
