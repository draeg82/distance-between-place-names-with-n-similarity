"""Looking a typed name up: the places that hold it, and the one place a search starts from.

A name is not a place (#9). `Springfield` is a question about eleven letters that a great many
places answer, and a tool that picked one of them would be answering a question nobody asked. So
the lookup hands over the whole list, largest place first, with the country beside each, and the
person picks. Only a `geonameid` - a specific place - can be searched from.

These rules are the seam's: which spelling a lookup answers, in what order, and what happens when
nothing, or nobody, matches. `tests/test_http_search.py` and `tests/test_browser_app.py` check
that the two adapters present this and add nothing of their own to it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from support import dump_row, place_database

from distnsimilarity.core.place_lookup import (
    MINIMUM_LOOKUP_TEXT,
    InvalidLookupText,
    PlaceNotFound,
    matches_name_exactly,
    order_for_reading,
    places_called,
)
from distnsimilarity.core.places import Place
from distnsimilarity.db.place_lookup import place_by_geonameid, places_matching_name
from distnsimilarity.db.schema import connect
from distnsimilarity.search import InvalidSearchRequest, SearchRequest, open_search

#: Four places called `Springfield`, at four sizes and in three countries, plus the three names a
#: lookup must not confuse them with: one whose name differs by a character and a space
#: (`Spring field`), one that the typed name is the beginning of (`Springfields`), and one that
#: differs only in case.
SPRINGFIELD_IL = 4_570_533
SPRINGFIELD_MO = 4_393_217
SPRINGFIELD_AU = 2_146_296
SPRINGFIELD_SMALL = 6_106_860
SPRING_FIELD = 1_111_111
SPRINGFIELDS = 2_222_222
SPRINGFIELD_LOWER = 3_333_333
LIMA_PE = 3_936_456
LIMA_US = 4_561_278

ROWS = [
    dump_row(
        SPRINGFIELD_IL, "Springfield", 39.7817, -89.6501, population=1_164_916, countrycode="US"
    ),
    dump_row(
        SPRINGFIELD_MO, "Springfield", 37.2089, -93.2923, population=169_751, countrycode="US"
    ),
    dump_row(
        SPRINGFIELD_AU, "Springfield", -27.6167, 153.0764, population=44_916, countrycode="AU"
    ),
    dump_row(SPRINGFIELD_SMALL, "Springfield", 43.0, -80.0, population=4_000, countrycode="CA"),
    dump_row(SPRING_FIELD, "Spring field", 43.1, -80.1, population=30_000, countrycode="CA"),
    dump_row(SPRINGFIELDS, "Springfields", 39.0, -89.0, population=20_000, countrycode="US"),
    dump_row(SPRINGFIELD_LOWER, "springfield", 40.0, -79.0, population=10_000, countrycode="US"),
    dump_row(LIMA_PE, "Lima", -12.0464, -77.0428, population=9_750_000, countrycode="PE"),
    dump_row(LIMA_US, "Lima", 40.5736, -84.1084, population=42_000, countrycode="US"),
]


def place(
    geonameid: int,
    asciiname: str,
    latitude: float,
    longitude: float,
    *,
    population: int = 1_000,
    country_code: str = "US",
) -> Place:
    return Place(
        geonameid=geonameid,
        asciiname=asciiname,
        display_name=asciiname,
        latitude=latitude,
        longitude=longitude,
        population=population,
        country_code=country_code,
    )


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return place_database(tmp_path / "places.sqlite3", ROWS)


@contextmanager
def opened(database: Path) -> Iterator[sqlite3.Connection]:
    """A connection, closed at the end of the assertion that needed it."""
    connection = connect(database)
    try:
        yield connection
    finally:
        connection.close()


def ids(places: object) -> list[int]:
    return [place.geonameid for place in places]


class TestATypedNameIsANameAndNotAPlace:
    """AC: "typing a name that matches more than one place offers a list of the matching places
    to choose from", "a name that matches no place shows that no place matches it"."""

    def test_a_typed_name_answers_with_every_place_called_exactly_that(self, database) -> None:
        with opened(database) as places:
            assert ids(places_matching_name(places, "Springfield")) == [
                SPRINGFIELD_IL,
                SPRINGFIELD_MO,
                SPRINGFIELD_AU,
                SPRINGFIELD_SMALL,
            ], "four places, in two countries, of four different sizes"

    def test_a_typed_name_is_not_the_beginning_of_a_name(self, database) -> None:
        """A prefix lookup would answer about places nobody typed: `Springfield` is not
        `Springfields`, and `Spring` is neither of them."""
        with opened(database) as places:
            assert SPRINGFIELDS not in ids(places_matching_name(places, "Springfield"))
            assert ids(places_matching_name(places, "Spring")) == []
            assert ids(places_matching_name(places, "Lim")) == []

    def test_the_spaces_around_a_name_are_not_part_of_it(self, database) -> None:
        with opened(database) as places:
            assert ids(places_matching_name(places, "  Lima \n")) == [LIMA_PE, LIMA_US]

    def test_case_is_a_difference_in_a_lookup_as_it_is_in_a_match(self, database) -> None:
        """The ASCII name is what matching compares, and case is a difference in it, so a lookup
        that folded case would offer places the search could not then be run from."""
        with opened(database) as places:
            assert ids(places_matching_name(places, "springfield")) == [SPRINGFIELD_LOWER]
            assert SPRINGFIELD_IL not in ids(places_matching_name(places, "springfield"))

    def test_a_name_no_place_holds_is_an_answer_of_nothing_rather_than_an_error(
        self, database
    ) -> None:
        with opened(database) as places:
            assert places_matching_name(places, "Springfielde") == ()

    def test_a_name_nobody_could_have_meant_is_refused_before_it_is_looked_up(
        self, database
    ) -> None:
        """AC: "the place-name field requires at least two characters". Refused rather than
        answered with nothing: a one-letter answer of `[]` would tell a person that no place is
        called `S`, and that is not what was asked."""
        with opened(database) as places:
            for text in ("", "S", " "):
                with pytest.raises(InvalidLookupText, match="at least 2 characters"):
                    places_matching_name(places, text)

    def test_the_shortest_text_a_lookup_answers_is_two_characters(self) -> None:
        assert MINIMUM_LOOKUP_TEXT == 2

    def test_a_lookup_is_asked_with_text_and_not_with_a_place_id(self, database) -> None:
        """A name and a `geonameid` are two different questions - one is answered with a list,
        the other with the single place a search starts from - so neither is accepted as the
        other."""
        with opened(database) as places:
            for wrong in (None, 42, ["Lima"]):
                with pytest.raises(InvalidLookupText, match="asked with the name itself"):
                    places_matching_name(places, wrong)  # type: ignore[arg-type]

    def test_the_rule_lives_in_the_seam_and_not_in_the_query(self) -> None:
        """`core.place_lookup` decides which spelling a name answers; `db.place_lookup` reads."""
        assert matches_name_exactly("Springfield", "Springfields") is False
        assert matches_name_exactly("Springfield", "Springfield") is True
        assert matches_name_exactly("Springfield", "springfield") is False
        choices = [
            place(SPRINGFIELD_IL, "Springfield", 39.78, -89.65),
            place(SPRINGFIELDS, "Springfields", 39.0, -89.0),
            place(SPRINGFIELD_LOWER, "springfield", 40.0, -79.0),
        ]
        assert ids(places_called(choices, "Springfield")) == [SPRINGFIELD_IL]


class TestTheListOfMatchesIsOrderedForChoosing:
    def test_the_biggest_place_is_offered_first(self, database) -> None:
        """AC: the list "identifies each place by country, population and coordinates", in an
        order a person can act on: the one they most likely mean is at the top."""
        with opened(database) as places:
            matches = places_matching_name(places, "Springfield")
        assert [found.population for found in matches] == [1_164_916, 169_751, 44_916, 4_000]

    def test_the_id_breaks_a_tie_so_the_same_lookup_cannot_reorder_itself(self) -> None:
        equals = [
            place(9, "Springfield", 1.0, 1.0, population=100),
            place(4, "Springfield", 2.0, 2.0, population=100),
            place(7, "Springfield", 3.0, 3.0, population=100),
        ]
        assert [found.geonameid for found in order_for_reading(equals)] == [4, 7, 9]

    def test_a_place_that_shares_a_name_is_still_a_separate_place(self, database) -> None:
        """The two Limas are two places, and the list says which is which by country."""
        with opened(database) as places:
            matches = places_matching_name(places, "Lima")
        assert [(found.country_code, found.population) for found in matches] == [
            ("PE", 9_750_000),
            ("US", 42_000),
        ]

    def test_the_answer_is_places_with_their_coordinates(self, database) -> None:
        with opened(database) as places:
            [biggest, *_] = places_matching_name(places, "Springfield")
        assert isinstance(biggest, Place)
        assert biggest.geonameid == SPRINGFIELD_IL
        assert (biggest.latitude, biggest.longitude) == (39.7817, -89.6501)
        # The length is what a sweep reads to pick its partner set; the display name is what a
        # reader is shown, and neither is the other.
        assert biggest.name_length == len("Springfield")
        assert biggest.display_name == "Springfield"


class TestReadingOnePlaceOut:
    def test_a_geonameid_is_read_back_as_the_place_it_names(self, database) -> None:
        with opened(database) as places:
            selected = place_by_geonameid(places, SPRINGFIELD_MO)
        assert (selected.asciiname, selected.population, selected.country_code) == (
            "Springfield",
            169_751,
            "US",
        )

    def test_a_geonameid_nobody_has_is_no_place_at_all(self, database) -> None:
        with opened(database) as places:
            assert place_by_geonameid(places, 999_999_999) is None

    def test_a_place_is_chosen_by_a_number_and_not_by_the_name_typed(self, database) -> None:
        for wrong in ("4570533", None, 1.5, True):
            with opened(database) as places:
                with pytest.raises(TypeError, match="GeoNames id"):
                    place_by_geonameid(places, wrong)

    def test_a_selected_place_is_a_place_and_not_the_name_typed(self, database) -> None:
        """AC: "searching from a place whose name is shared uses the selected place's own
        coordinates". Four places share every character of `Springfield` and answer to one name,
        so the only thing a search can measure from is the place that was picked."""
        with opened(database) as places:
            matches = places_matching_name(places, "Springfield")
        assert len(matches) == 4
        assert len({(found.latitude, found.longitude) for found in matches}) == 4
        assert len({found.geonameid for found in matches}) == 4

    def test_a_lookup_on_a_database_with_no_places_answers_with_nothing(
        self, tmp_path: Path
    ) -> None:
        """An empty place database is refused by the seam itself (`CacheNotImportedError`) before
        any search, so a lookup is only ever asked the question it can answer."""
        empty = place_database(tmp_path / "empty.sqlite3", [])
        with opened(empty) as places:
            assert places_matching_name(places, "Springfield") == ()
            assert place_by_geonameid(places, SPRINGFIELD_IL) is None


class TestANameLookupIsAnsweredByAnIndex:
    """AC: "a lookup by name completes in under a second against the full 15 000 dataset". A
    name lookup is the one read in this tool that a person waits on before a search even
    starts, so it is pinned to the index rather than to a stopwatch."""

    def plan(self, database: Path, sql: str) -> str:
        with sqlite3.connect(database) as connection:
            rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        return " | ".join(str(row[-1]) for row in rows)

    def test_a_lookup_by_name_is_answered_by_the_index_on_asciiname(self, database) -> None:
        plan = self.plan(
            database, "SELECT geonameid FROM place WHERE asciiname = 'Springfield'"
        )
        assert "idx_place_asciiname" in plan, f"the lookup walks the table, not the index: {plan}"

    def test_a_place_read_by_id_is_answered_by_the_primary_key(self, database) -> None:
        plan = self.plan(database, "SELECT geonameid FROM place WHERE geonameid = 4570533")
        assert "PRIMARY KEY" in plan.upper(), plan

    def test_the_places_a_sweep_walks_are_answered_by_the_index_on_name_length(
        self, database
    ) -> None:
        plan = self.plan(database, "SELECT geonameid FROM place WHERE name_length = 11")
        assert "idx_place_name_length_population" in plan, (
            f"a sweep would read every place to find the ones it can compare: {plan}"
        )


class TestSearchingFromOneChosenPlace:
    """The seam is handed a place, never a name. AC: "choosing one of them searches from that
    place", "choosing a place whose geonameid does not exist answers with an error rather than a
    blank search"."""

    def test_a_request_names_the_place_it_starts_from(self, database) -> None:
        session = open_search(SearchRequest(from_place=SPRINGFIELD_MO, database=database))
        try:
            assert session.origin is not None
            assert session.origin.geonameid == SPRINGFIELD_MO
            assert session.origin.latitude == 37.2089
        finally:
            session.close()

    def test_a_place_nobody_has_is_refused_rather_than_searched_from(self, database) -> None:
        """A blank search would say `Springfield` is 169 km from nothing. The truth is that 42 is
        not a place, and the reader has to be told which of their two beliefs is wrong."""
        with pytest.raises(PlaceNotFound, match="42"):
            open_search(SearchRequest(from_place=42, database=database))

    def test_a_search_from_a_place_names_no_single_character_difference(self) -> None:
        """A threshold and a range are two different questions about the same two names."""
        with pytest.raises(InvalidSearchRequest, match="character difference"):
            SearchRequest(n=2, from_place=42)

    def test_a_search_that_names_neither_a_place_nor_a_threshold_is_not_a_search(self) -> None:
        with pytest.raises(InvalidSearchRequest, match="character difference"):
            SearchRequest()

    def test_a_place_is_a_number_and_not_a_name(self) -> None:
        with pytest.raises(InvalidSearchRequest, match="GeoNames id"):
            SearchRequest(from_place="Springfield")  # type: ignore[arg-type]
