# Which half of a pair is first follows the order the candidates arrive in

A reported pair has a first place and a second place, and that order used to be the dump's:
places in decreasing population, so the bigger place came first. Candidate generation moved
onto the database, which walks a name-length group in increasing population - the order the
keyset paging needs, and the only order that keeps a long search a stream rather than a
sort-then-slice. So the two paths disagree about direction: the same pair of places comes out
as `(1, 2)` from a dump and `(2, 1)` from the database.

The direction is not normalised. Which place is named first carries the order the search
walked, and a caller who sees results arriving can use it; rewriting pairs into id order
would hide the difference rather than decide anything.

## Consequences

Comparing a database scan against a dump scan has to drop the direction first, and the tests
say so out loud: `unordered_signature()` in `tests/test_database_candidates.py` matches pairs
by which two places and what distance, and
`test_a_pair_is_oriented_by_the_candidate_order` pins the two directions themselves so that
neither can change without a test noticing.

The terminal adapter still reads dumps, so the CSV a person compares against the original
tool's output is unchanged, byte for byte, and the terminal's tests keep that order pinned.
When a database-backed adapter writes the same CSV, its rows arrive in database order, and it
will have to say which order it expects rather than inherit one.

Distances are unaffected: a great-circle distance is symmetric, so a pair's distance is the
same number in either direction, to the last bit.
