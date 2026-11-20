# A name lookup answers with every place and never chooses

A typed name is not a place. `Springfield` belongs to more than a hundred places in the
cities1000 cache, spread over several countries, and the whole answer of a from-a-place search
is measured from wherever it starts: the distance to a near name depends on which of those
hundred places the search is run from. So a lookup that turned a typed name into a place had
already made the one choice in the sequence that belongs to the reader, and it made it with no
way for anyone to see that it had been made.

A **place lookup** matches by equality with the ASCII name — no prefix, no case fold, two
characters at least — and returns every place holding that name, ordered by population with the
GeoNames id breaking ties. It answers the question a typed name can actually support, which is
"which places is this a name of", and stops there. A search is only ever run from a
`geonameid`, never from a name: the selected place is an id taken out of a place lookup, and the
lookup's job ends at the list it returns.

## Consequences

The app must list and the reader must choose. Taking the first row silently — the biggest place,
the first id, whichever the query happened to return first — is refused by this decision and not
by UI taste, so a name that belongs to one place still arrives as a list of one rather than
being resolved away. A search that could be started from a name would carry that choice in its
request, and two requests naming the same name could mean different places.

The lookup's comparison is the comparison the matcher uses: equality between ASCII names, with an
accented letter a different character from the plain letter it folds to and case a difference.
That is why the list of choices and the search that follows cannot disagree about which places a
name is about. A lookup that folded case, or matched loosely, would offer places the subsequent
sweep cannot produce pairs for, and the reader would be choosing between options the search
disagrees with.

Prefix matching is refused rather than offered. It answers about places nobody typed, and it gets
slower as the cache grows: equality on an ASCII name is the lookup the place database is arranged
for (ADR 0003), while a prefix reads more of the table every time a place is added. The two
character floor is the smallest query this will answer, so a single keystroke is treated as a
name that is not finished rather than as a name nobody has.

"No place is called that" and "no place database" are different sentences, and each gets its own.
The first is an empty list — a lookup that completed and found nothing, which is a fact about the
name. The second is a status code, because there is nothing to look in: the place database is the
only source (ADR 0007), and a search that silently reads something else is worse than one that
refuses. An id no place in this database holds is refused the same way, and not answered as a
search with an empty list: the selected place is missing, which is not the same as a place with
no near names.
