# A from-a-place search sweeps a range that starts at zero

Everywhere mode asks for exactly one character difference: a threshold, matched exactly, never
at most and never at least (ADR 0001). From-a-place mode asks a different question, and it asks
it about one selected place. A reader who wants to know what `Springfield` could be confused
with wants the names one character off, the names two characters off, and the names that are the
same, all at once; one threshold at a time turns one question into `difference_limit` searches
and leaves the reader to add the answers up. The shape of the work settles how it has to run:
the candidates for one name are every equal-length place in the candidate set, which runs to
thousands in the cities1000 cache, so the answer has to arrive in well under a second, and a
walk per value of the range would pay for that walk as many times as there are values.

A from-a-place search is therefore a **sweep**: the selected place walked against the candidates
it can be compared with in one pass, over the whole character-difference range
`0..difference_limit`, reporting a count for each value of the range beside the list of pairs.
The range is a limit and not a width — it always starts at zero, and the limit says how far it
goes, never how wide it is. Everywhere mode keeps its single exact threshold, and a range never
belongs to it: `difference_limit` is not a second spelling of `threshold`, and a request that
sends one to an everywhere search is refused rather than read as the other.

## Consequences

Zero is a value like any other, and that is what makes identical names reachable at all. Two
places sharing one ASCII name differ at no positions, so a shared name is a character difference
of zero; a range that started at one would put every duplicate name out of reach of the one mode
a reader uses to look for near names, and would report its absence as a finding rather than as a
rule. The pair a place makes with itself is the same value at a distance of nothing.

`exclude_identical_names` changes only what a sweep *reports*. It never changes what is compared
or what is counted, so the count at zero keeps filling up while the pairs behind it are left out
of the list, and the count says it was excluded. A switch that quietly narrowed the range would
make the counts describe a search nobody asked for.

The per-value counts and the rows come off the same walk, so they cannot disagree about what a
sweep found. A count from a second pass over the candidates is the alternative this refuses: it
would let a list and its own totals come from different walks, and a stopped sweep would have to
explain which of the two had gone further. The counts are half the answer, which is why they are
produced while the pairs are, and a sweep that is stopped reports how far it got the same way a
scan does (ADR 0006).

One walk means one candidate set, so the population floor and the name-length group decide a
sweep exactly as they decide a scan (ADR 0003, ADR 0004): a place below the floor is never
fetched, never compared, and never counted at any value of the range. The order of the list
remains a property of the walk, not a promise (ADR 0005).
