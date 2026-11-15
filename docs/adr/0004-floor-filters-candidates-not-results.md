# The population floor decides what is compared, not what is shown

A minimum-population selector could filter results on the way out, which is the cheaper
change: run the search, then hide the pairs whose places are too small. It is not taken,
because it would keep paying for every comparison the caller did not want and would report
counts of pairs that were found and then thrown away.

Instead the floor is applied while the candidate set is built - in SQL, as
`population >= ?` next to the name-length lookup - so a place below the floor is never
fetched, never grouped, and never compared.

## Consequences

A higher floor makes a search genuinely shorter. On the merged 146,782 places, a floor of 0
compares 927,424,318 pairs; a floor of 5,000 leaves 55,108 places and 139,850,045 pairs, about
a sixth of the work; a floor of 100,000 leaves 5,072 places and 1,420,897 pairs. Those
numbers come from `scope_for()`, which is the same query the interface will show before a
search starts, so what a person is told and what a search does cannot drift apart.

The floor is inclusive: a place whose population is exactly 5,000 is a candidate at a floor of
5,000. GeoNames populations are counts of people, and the dumps' own cuts are inclusive, so the
boundary belongs to the search.

The floor is the same number as a dump threshold, and `dataset_threshold()` accepts either
spelling - `5000`, `"5000"`, `"cities5000.txt"` - so "the cities5000 set" and "population at
least 5000" select one set of places. They are not the same *set of rows*: the number in a dump
name is a cut the file was made at, not a promise about the rows inside it. Applying a floor of
5,000 to the merged table selects 55,108 places, while `cities5000.txt` holds 55,700 rows - 677
of them below 5,000, and 85 places at or above 5,000 missing from the file. The floor is
computed from populations, never from which file a place arrived in.

Because the floor is applied before any pair is compared, a floor can remove a pair entirely
rather than shorten it. Two places at 5,000 and 6,000 form a pair at a floor of 5,000 and no
pair at 5,001; nothing is found-then-hidden in between, and no pair appears at a floor that
excludes one of its places.
