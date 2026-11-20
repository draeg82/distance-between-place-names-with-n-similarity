"""Looking a place name up: which places a typed name is a question about.

A name is not a place. `Springfield` is a question about eleven letters that more than a hundred
places in the cities1000 dataset answer, spread across several countries, and the distance between
two of them is the interesting part. So a lookup answers with the whole list and a person picks:
only a `geonameid` - one specific place - can be searched *from*.

Two rules decide what a lookup answers, and both are here rather than in a query so that they can
be argued with:

* a typed name is matched by **equality** with an ASCII name, never by its beginning. A prefix
  lookup would answer about places nobody typed - `Springfield` is not `Springfields`, and typing
  `Spring` is a question about neither. Case is a difference in an ASCII name exactly as it is in a
  match, so a lookup that folded case would offer places the search could not then be run from.
* the spaces around a name are not part of it; two characters are the least that counts as one. A
  single letter is a keystroke, not a place being asked about, and answering it would be a list
  nobody can read.

Reading happens in `db.place_lookup`; this module holds no SQL and reads no file, which is what
lets the same rules be checked against a list of places in memory.
"""

from __future__ import annotations

from collections.abc import Iterable

from distnsimilarity.core.places import Place

#: The shortest text a lookup answers. One character is not a name being asked about: it matches a
#: scattering of unrelated places, and a person who types one has not started asking yet.
MINIMUM_LOOKUP_TEXT = 2


class PlaceNotFound(ValueError):
    """A `geonameid` that no place in this database holds.

    This is the refusal behind "choosing a place answers with an error rather than a blank search":
    a search from a place that does not exist would report a name at no distance from nothing,
    which reads like an answer and is not one.
    """


class InvalidLookupText(ValueError):
    """Text too short to be a place being asked about.

    Its own type because an adapter has to answer it: "two characters at least" is a refusal of a
    question, which a browser shows next to the field, and not a failure of the lookup.
    """


def lookup_text(text: object) -> str:
    """The name a lookup will answer for, or a refusal naming the rule it broke."""
    if not isinstance(text, str):
        raise InvalidLookupText(
            f"a place lookup is asked with the name itself, got a {type(text).__name__}: "
            f"{text!r}"
        )
    typed = text.strip()
    if len(typed) < MINIMUM_LOOKUP_TEXT:
        raise InvalidLookupText(
            f"a place lookup needs at least {MINIMUM_LOOKUP_TEXT} characters, got {text!r}"
        )
    return typed


def matches_name_exactly(typed: str, asciiname: str) -> bool:
    """Is this place called exactly what was typed?

    Equality, case-sensitively, with no prefix and no fold: the same comparison a pair of names is
    matched by, so the list of choices and the search that follows cannot disagree about which
    places a name is about.
    """
    return typed == asciiname


def order_for_reading(places: Iterable[Place]) -> tuple[Place, ...]:
    """The order the choices are offered in: biggest place first, GeoNames id to break a tie.

    A list of same-named places is read top-down until a country code matches what the person had
    in mind, so the one they most likely mean goes at the top. Population is the only signal this
    tool has for "the one you mean", and the id makes the order total: two places of equal size
    cannot trade places between one lookup and the next.
    """
    return tuple(sorted(places, key=lambda place: (-place.population, place.geonameid)))


def places_called(places: Iterable[Place], text: str) -> tuple[Place, ...]:
    """The places in `places` called exactly `text`, offered in the order a person reads them.

    The rule an interface would otherwise re-implement over whatever list it was handed - and the
    reason a lookup of `Spring` finds nothing rather than every `Spring-` anything.
    """
    typed = lookup_text(text)
    return order_for_reading(
        place for place in places if matches_name_exactly(typed, place.asciiname)
    )
