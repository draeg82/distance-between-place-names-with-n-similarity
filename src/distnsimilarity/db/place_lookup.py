"""Reading a name lookup out of the place database.

Two reads, both answered by an index and neither by a walk over the table:

* every place whose ASCII name is exactly what was typed - the choices a person picks a search
  from, on `idx_place_asciiname`;
* the one place a `geonameid` names - the search's starting point, on the primary key.

Neither one applies a population floor. A name lookup answers a question about spelling, and the
floor a search runs at decides what the selected place is *compared with*, not which places the
name is about: leaving a 4,000-person Springfield out of the list would hide a place a person can
correctly choose to search from.

The rules behind these reads - equality, case, how short a name may be, the order the choices are
offered in - are in `core.place_lookup`, so that a query cannot quietly become the definition.
"""

from __future__ import annotations

import sqlite3

from distnsimilarity.core.place_lookup import lookup_text, order_for_reading
from distnsimilarity.core.places import Place
from distnsimilarity.db.schema import PLACE_SELECT, row_to_place


def places_matching_name(connection: sqlite3.Connection, text: str) -> tuple[Place, ...]:
    """Every place called exactly `text`, biggest first.

    `asciiname` is compared with SQLite's own byte-for-byte collation, which is the only spelling
    of "exactly" that agrees with the matching rule: no case fold, no prefix, no accent fold. The
    name is a full value on both sides, so the lookup is an index seek whatever it matches - a
    `LIKE 'Springfield%'` would turn the shortest question in the tool into a table scan.
    """
    typed = lookup_text(text)
    rows = connection.execute(
        f"{PLACE_SELECT} WHERE asciiname = ?", (typed,)
    ).fetchall()
    # Ordered here rather than in SQL so that one rule says how the choices are offered, to this
    # reader and to the next one. A name belongs to a handful of places, not to a hundred thousand.
    return order_for_reading(row_to_place(row) for row in rows)


def place_by_geonameid(connection: sqlite3.Connection, geonameid: object) -> Place | None:
    """The place a `geonameid` names, or None when no place holds it.

    The id is the only way a name becomes a place, so this is the read every from-a-place search
    starts from - and the one that can say "that is not a place", which is the difference between
    a search and an empty one.
    """
    if isinstance(geonameid, bool) or not isinstance(geonameid, int):
        raise TypeError(
            f"a place is chosen by its GeoNames id, got a {type(geonameid).__name__}: "
            f"{geonameid!r}"
        )
    row = connection.execute(
        f"{PLACE_SELECT} WHERE geonameid = ?", (geonameid,)
    ).fetchone()
    return None if row is None else row_to_place(row)
