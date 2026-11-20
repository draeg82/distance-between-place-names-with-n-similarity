"""Looking a name up over HTTP, then sweeping outward from the place that was chosen.

Issue #9's two questions on the same wire as #6. `/v1/places` answers the one the rest of this API
has never had to answer: *which places* is this a name for? Every one of them comes back with the
country, population and coordinates that tell them apart, and nothing in the answer says which one
the caller meant, because which one the caller meant is the whole feature. `/v1/search` then takes
one `geonameid` and sweeps a range of character differences outward from it - the same stream, the
same four events and the same rows as a search from everywhere, with the place it starts from in the
opening event and what each value of the range found alongside every event after it.

The fixtures are two different shapes on purpose. The Lima family (in `tests/support.py`, because
the seam and the page ask about the same three Limas) is one name in three countries: the answer a
range that starts at zero exists for. The Springfield family is one name in four spellings - a
lower-case first letter, a plural, a space - where only three of them hold the name that was typed:
the answer a lookup gives, and the proof that the lookup matches exactly one spelling, the same one
the matching rules match.

One class is `@pytest.mark.slow` and runs against the committed `cities1000.txt`, because "results
return in well under a second for a representative single-name search" is not a thing a thirteen
place fixture can claim. The times it comes back with are recorded on the ticket.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from distnsimilarity.api import asking_words
from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.sweep import DEFAULT_DIFFERENCE_LIMIT
from distnsimilarity.search import (
    EVERYWHERE_MODE,
    FROM_PLACE_MODE,
    SearchRequest,
    open_search,
)
from http_support import (
    differences_of,
    done_of,
    events_of,
    excluded_of,
    of_type,
    serve,
    started_of,
    types_of,
)
from support import (
    BOBA,
    LIMA_CA,
    LIMA_PE,
    LIMA_US,
    LINA,
    LINO,
    dump_row,
    lima_family_rows,
    place_database,
)
from test_database_candidates import CITIES_1000

#: One name, in the four spellings somebody could type it in.
#:
#: Three places hold the spelling that would be typed. `springfield` is the same word with a
#: lower-case first letter: one differing position, and a different name rather than a softer
#: spelling of the same one. `Springfields` and `Spring field` are twelve characters, so they have
#: no character difference with `Springfield` at all - which is why a sweep from an eleven letter
#: name never reads them.
SPRINGFIELD_IL = 4_409_896
SPRINGFIELD_MA = 4_951_788
SPRINGFIELD_MO = 4_250_542
SPRINGFIELD_LOWER = 7_100_001
SPRINGFIELDS = 7_100_002
SPRING_FIELD = 7_100_003

#: What a sweep from Lima, Peru finds, in the order the ranking puts them at greatest distance
#: first: `Boba` (three positions), `Lino` (two), `Lina` (one), then the Limas in North America
#: (none). Reversed, this is the nearest-first answer.
FINDS_FROM_PERU = [BOBA, LINO, LINA, LIMA_CA, LIMA_US]
DISTANCES_FROM_PERU = [12_303.218, 11_186.930, 9_921.888, 6_128.289, 5_896.192]


def lookup_rows() -> list[dict[str, Any]]:
    """The Limas to sweep from and the Springfields to look a name up in, in one cache."""
    return [
        *lima_family_rows(),
        dump_row(
            SPRINGFIELD_IL, "Springfield", 39.7817, -89.6501, population=166_810, countrycode="US"
        ),
        dump_row(
            SPRINGFIELD_MA, "Springfield", 42.1015, -72.5898, population=154_341, countrycode="US"
        ),
        dump_row(
            SPRINGFIELD_MO, "Springfield", 37.2089, -93.2923, population=116_565, countrycode="US"
        ),
        dump_row(SPRINGFIELD_LOWER, "springfield", 41.0, -73.0, population=9_000, countrycode="US"),
        dump_row(SPRINGFIELDS, "Springfields", 41.5, -73.5, population=5_000, countrycode="US"),
        dump_row(SPRING_FIELD, "Spring field", 42.0, -74.0, population=4_000, countrycode="US"),
    ]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """One cache for both questions, because the two endpoints have to agree about its places."""
    return place_database(tmp_path / "places.sqlite3", lookup_rows())


def rows_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last `results` event's rows: the best list this search ended with."""
    results = of_type(events, "results")
    assert results, f"the stream never sent a list: {types_of(events)}"
    return list(results[-1]["results"])


class TestLookingANameUp:
    """`GET /v1/places?name=...`: every place a typed name is a question about."""

    def test_a_name_answers_with_every_place_holding_it(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.places("Springfield")

        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["name"] == "Springfield"
        assert body["match_count"] == 3
        assert [match["geonameid"] for match in body["matches"]] == [
            SPRINGFIELD_IL,
            SPRINGFIELD_MA,
            SPRINGFIELD_MO,
        ], "the places holding the name, biggest first: the one a reader most likely means is offered first"

    def test_every_match_carries_what_it_takes_to_tell_two_places_apart(
        self, database: Path
    ) -> None:
        """AC: "every matching place, each with country, population and coordinates"."""
        with serve(database) as server:
            body = server.places("Springfield").json()

        assert body["matches"][0] == {
            "geonameid": SPRINGFIELD_IL,
            "asciiname": "Springfield",
            "display_name": "Springfield",
            "latitude": 39.7817,
            "longitude": -89.6501,
            "population": 166_810,
            "country_code": "US",
        }, "a list of names cannot be chosen from: the country, the population and the position are the choice"

    def test_a_name_shared_between_countries_answers_with_all_of_them(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            body = server.places("Lima").json()

        assert [match["geonameid"] for match in body["matches"]] == [LIMA_PE, LIMA_US, LIMA_CA]
        assert sorted(match["country_code"] for match in body["matches"]) == ["CA", "PE", "US"], (
            "the same name in three countries, and the search would measure from a different one "
            "of them every time"
        )

    def test_the_answer_is_the_list_and_never_a_choice(self, database: Path) -> None:
        """AC: "the app never silently takes the first match", starting at the end that could.

        There is no field on this answer that could be read as a recommendation, and no second
        endpoint that takes a name and searches it: the only way to get a search is to send back one
        of these `geonameid`s.
        """
        with serve(database) as server:
            body = server.places("Springfield").json()

        assert sorted(body) == ["database", "match_count", "matches", "name"]
        assert all(sorted(match) == sorted(body["matches"][0]) for match in body["matches"])

    def test_the_spelling_that_matches_is_the_spelling_the_search_matches(
        self, database: Path
    ) -> None:
        """Case is a difference, so `springfield` is a different name and not the same one softer.

        The same rule the matching uses: a lookup that folded case would offer places whose names
        the search then refuses to call a match, and the two answers would disagree by one letter.
        """
        with serve(database) as server:
            lower = server.places("springfield").json()
            typed = server.places("Springfield").json()

        assert [match["geonameid"] for match in lower["matches"]] == [SPRINGFIELD_LOWER]
        assert SPRINGFIELD_LOWER not in [m["geonameid"] for m in typed["matches"]]

    def test_a_name_that_is_only_part_of_a_name_matches_nothing(self, database: Path) -> None:
        """`Spring` is not `Springfield` in a shorter spelling, and a prefix search would say it is.

        A prefix would also be the answer that gets slower as the cache grows, which is the wrong
        reason to prefer it but the same outcome: this lookup is one index seek at any size.
        """
        with serve(database) as server:
            prefix = server.places("Spring").json()
            plural = server.places("Springfields").json()

        assert (prefix["match_count"], prefix["matches"]) == (0, [])
        assert [match["geonameid"] for match in plural["matches"]] == [SPRINGFIELDS]

    def test_a_name_no_place_holds_is_an_empty_list_rather_than_an_error(
        self, database: Path
    ) -> None:
        """AC: "no place matches that name" is an answer, and a different one from "no results".

        200 with nothing in it, because a 404 here would be the same status the server uses for a
        place id that does not exist, and the page has two different sentences to say.
        """
        with serve(database) as server:
            answer = server.places("Nowhere")

        assert answer.status_code == 200, answer.text
        assert answer.json()["match_count"] == 0
        assert answer.json()["matches"] == []

    def test_a_name_too_short_to_ask_with_is_refused_before_anything_is_read(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            answer = server.places("S")

        assert answer.status_code == 422, answer.text
        assert answer.json()["error"] == "InvalidPlaceLookup"
        assert "2 characters" in answer.json()["message"], (
            f"the refusal has to say how long a name has to be: {answer.json()['message']}"
        )

    def test_a_lookup_with_no_name_at_all_says_what_a_lookup_asks_for(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            answer = server.get("/v1/places")

        assert answer.status_code == 422, answer.text
        body = answer.json()
        assert body["error"] == "InvalidPlaceLookup"
        assert "name" in body["message"] and "/v1/places" in body["message"], (
            f"and where to put it: {body['message']}"
        )

    def test_a_lookup_that_finds_no_database_says_which_import_to_run(
        self, tmp_path: Path
    ) -> None:
        """An empty list is the answer to "no place is called that"; this is a different failure.

        Answering this one with `matches: []` would leave the reader choosing from a list the
        server never had, so it is a status code and the command to run.
        """
        missing = tmp_path / "places.sqlite3"

        with serve(missing) as server:
            answer = server.places("Lima")

        assert answer.status_code == 409, answer.text
        body = answer.json()
        assert body["error"] == "PlaceDatabaseNotImported"
        assert "distnsimilarity-import" in body["message"]
        assert str(missing) in body["message"]
        assert body["database"]["imported"] is False

    def test_the_name_comes_back_without_the_spaces_a_typer_left(self, database: Path) -> None:
        with serve(database) as server:
            body = server.get("/v1/places", name="  Lima  ").json()

        assert body["name"] == "Lima" and body["match_count"] == 3

    def test_the_parameter_that_no_longer_exists_is_refused_here_too(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            answer = server.get("/v1/places", name="Lima", source="cities5000")

        assert answer.status_code == 410, answer.text
        assert answer.json()["error"] == "RemovedParameter"

    def test_every_id_the_lookup_hands_out_is_an_id_the_search_takes(
        self, database: Path
    ) -> None:
        """The two ends of #9 meet here: a match becomes the place a sweep starts from.

        All three Limas, not just the biggest: whichever one a reader picks, the answer is measured
        from that place's own coordinates.
        """
        with serve(database) as server:
            offered = server.places("Lima").json()["matches"]
            for match in offered:
                events = events_of(server.search_from(match["geonameid"], difference_limit=0))
                started = started_of(events)
                assert started["origin"]["geonameid"] == match["geonameid"]
                assert started["origin"]["latitude"] == match["latitude"], (
                    "the sweep starts from the coordinates the lookup showed, not from another "
                    "place that happens to share the name"
                )


class TestSweepingFromAPlace:
    """`GET /v1/search?from_place=...`: the same stream, asked of one place's range."""

    def test_a_sweep_is_the_same_four_events_as_a_search_from_everywhere(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            swept = events_of(server.search_from(LIMA_PE))
            everywhere = events_of(server.search(1))

        for events in (swept, everywhere):
            kinds = types_of(events)
            assert kinds[0] == "started" and kinds[-1] == "done", kinds
            assert kinds.count("started") == 1 == kinds.count("done"), kinds
            assert set(kinds) <= {"started", "progress", "results", "done"}, kinds
            assert all(line["search_id"] for line in events)
        assert len(of_type(swept, "results")) >= 1, "the sweep has finds to report"

    def test_the_opening_event_names_the_place_the_search_starts_from(
        self, database: Path
    ) -> None:
        """`from_place` is what the caller sent; `origin` is the place the database had behind it."""
        with serve(database) as server:
            started = started_of(events_of(server.search_from(LIMA_PE, difference_limit=2)))

        assert started["mode"] == FROM_PLACE_MODE
        assert started["from_place"] == LIMA_PE
        assert started["n"] is None, "a sweep has no single threshold to echo"
        assert started["difference_limit"] == 2
        assert started["exclude_identical_names"] is False
        assert started["origin"] == {
            "geonameid": LIMA_PE,
            "asciiname": "Lima",
            "display_name": "Lima",
            "latitude": -12.0464,
            "longitude": -77.0428,
            "population": 9_750_000,
            "country_code": "PE",
        }

    def test_the_range_is_three_values_up_from_zero_unless_somebody_says(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            started = started_of(events_of(server.search_from(LIMA_PE)))

        assert started["difference_limit"] == DEFAULT_DIFFERENCE_LIMIT
        assert [row["character_difference"] for row in started["differences"]] == [0, 1, 2, 3], (
            "the shape of the answer is on the wire before the walk starts, and zero is in it"
        )

    def test_a_search_from_everywhere_says_which_mode_it_is_and_names_no_place(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            started = started_of(events_of(server.search(1)))

        assert started["mode"] == EVERYWHERE_MODE
        assert started["n"] == 1
        assert started["from_place"] is None
        assert started["origin"] is None
        assert started["difference_limit"] is None
        assert started["exclude_identical_names"] is None
        assert started["differences"] == [], (
            "everywhere mode asks for one threshold, so there is no range to report value by value"
        )

    def test_a_name_shared_across_countries_is_found_at_zero_differing_characters(
        self, database: Path
    ) -> None:
        """AC: "a name shared across countries produces results at differing-character count 0"."""
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE, difference_limit=0))

        rows = rows_of(events)
        assert sorted(row["second"]["geonameid"] for row in rows) == sorted([LIMA_US, LIMA_CA])
        assert sorted(row["second"]["country_code"] for row in rows) == ["CA", "US"]
        assert {row["character_difference"] for row in rows} == {0}
        assert all(row["differing_positions"] == [] for row in rows), (
            "nothing to highlight: the names are the same, which is the finding"
        )
        assert differences_of(done_of(events)) == {0: 2}

    def test_every_distance_is_measured_from_the_place_that_was_chosen(
        self, database: Path
    ) -> None:
        """AC: "every result's distance is measured from the selected place's coordinates".

        The three Limas are 5,896 km and 6,128 km from Lima, Peru and 434 km from each other, so
        only the chosen place's own position produces these numbers: measuring `Lima CA` from Ohio
        instead of Peru is an answer ten times smaller.
        """
        with serve(database) as server:
            from_peru = rows_of(events_of(server.search_from(LIMA_PE, difference_limit=0)))
            from_ohio = rows_of(events_of(server.search_from(LIMA_US, difference_limit=0)))

        peru = {row["second"]["geonameid"]: row["distance_km"] for row in from_peru}
        ohio = {row["second"]["geonameid"]: row["distance_km"] for row in from_ohio}
        assert peru[LIMA_CA] == pytest.approx(
            great_circle_distance_km(-12.0464, -77.0428, 43.0, -80.0)
        )
        assert ohio[LIMA_CA] == pytest.approx(434.443)
        assert ohio[LIMA_CA] < peru[LIMA_CA] / 10
        assert ohio[LIMA_PE] == pytest.approx(peru[LIMA_US]), (
            "the same pair, the same distance, whichever end the reader chose to stand on"
        )

    def test_each_value_of_the_range_says_what_it_found(self, database: Path) -> None:
        """AC: "the number of candidates produced by each character-difference value is shown"."""
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE))

        assert [row["character_difference"] for row in done_of(events)["differences"]] == [
            0,
            1,
            2,
            3,
        ], "the values are reported in range order, so a reader can read them as a range"
        assert differences_of(started_of(events)) == {0: 0, 1: 0, 2: 0, 3: 0}
        assert differences_of(done_of(events)) == {0: 2, 1: 1, 2: 1, 3: 1}
        assert differences_of(of_type(events, "results")[-1]) == differences_of(done_of(events)), (
            "the tally beside the list and the tally in the totals come from the same walk"
        )

    def test_a_value_that_found_nothing_is_still_in_the_answer(self, database: Path) -> None:
        """A range that comes back empty in the middle says so rather than shortening itself."""
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE, difference_limit=5, min_population=25_000))

        assert differences_of(done_of(events)) == {0: 1, 1: 1, 2: 0, 3: 0, 4: 0, 5: 0}
        assert [row["character_difference"] for row in done_of(events)["differences"]] == [
            0,
            1,
            2,
            3,
            4,
            5,
        ]

    def test_excluding_identical_names_leaves_the_rows_out_and_not_the_count(
        self, database: Path
    ) -> None:
        """AC: "an exclude identical names toggle"."""
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE, exclude_identical_names=True))

        rows = rows_of(events)
        assert sorted(row["second"]["geonameid"] for row in rows) == sorted([BOBA, LINO, LINA])
        assert {row["character_difference"] for row in rows} == {1, 2, 3}
        done = done_of(events)
        assert differences_of(done) == {0: 2, 1: 1, 2: 1, 3: 1}, (
            "the sweep still walked past the identical names, so it still says how many there were"
        )
        assert excluded_of(done) == [0]
        assert done["pairs"] == 3, "the list is three rows, and the excluded pair count is not in it"

    def test_the_place_a_sweep_starts_from_is_never_one_of_its_own_rows(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            rows = rows_of(events_of(server.search_from(LIMA_PE)))

        assert rows
        for row in rows:
            assert row["first"]["geonameid"] == LIMA_PE
            assert row["second"]["geonameid"] != LIMA_PE

    def test_a_row_from_a_sweep_has_the_same_cells_as_a_row_from_everywhere(
        self, database: Path
    ) -> None:
        """AC: "the same row content and highlights as everywhere mode", checked on the wire.

        One row shape for both searches, so the page draws one table rather than guessing which
        search sent the list it is holding.
        """
        with serve(database) as server:
            swept = rows_of(events_of(server.search_from(LIMA_PE)))[0]
            everywhere = rows_of(events_of(server.search(1)))[0]

        assert swept.keys() == everywhere.keys()
        assert swept["first"].keys() == everywhere["first"].keys()
        assert swept["differing_positions"] and everywhere["differing_positions"], (
            "and the highlight data is really there in both modes, not merely the same keys"
        )
        assert len(swept["differing_positions"]) == swept["character_difference"], (
            "the highlights are the differing positions, which is what makes a zero in a sweep and "
            "a zero in everywhere mode the same kind of row"
        )

    def test_the_finds_are_ranked_by_the_same_ranking_the_other_mode_uses(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            nearest = rows_of(events_of(server.search_from(LIMA_PE, direction="least")))
            furthest = rows_of(events_of(server.search_from(LIMA_PE, direction="greatest")))
            kept = rows_of(events_of(server.search_from(LIMA_PE, top_k=2)))

        assert [row["second"]["geonameid"] for row in furthest] == FINDS_FROM_PERU
        assert [row["second"]["geonameid"] for row in nearest] == list(reversed(FINDS_FROM_PERU))
        assert [row["distance_km"] for row in furthest] == pytest.approx(
            DISTANCES_FROM_PERU, abs=0.01
        )
        assert len(kept) == 2, "`top_k` is the same knob in both modes"
        assert kept[0]["second"]["geonameid"] == BOBA

    def test_the_sweep_walks_each_partner_once(self, database: Path) -> None:
        """A range of four values is one walk of the partners, not four walks of them."""
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE, difference_limit=5))

        done = done_of(events)
        assert done["pairs_compared"] == 5, (
            "five partners - the other four-letter names and the two other Springfields are "
            f"twelve and eleven letters - compared once each for six values of the range: "
            f"{done['pairs_compared']}"
        )
        assert differences_of(done) == {0: 2, 1: 1, 2: 1, 3: 1, 4: 0, 5: 0}

    def test_no_such_place_and_nothing_in_range_are_two_different_answers(
        self, database: Path
    ) -> None:
        """AC: the two empties are distinct, and the API is where that distinction starts.

        A name nothing holds is a list with no rows in it; a range that found nothing is a search
        that ran, compared something, and says what each value produced. A page can only draw those
        as two different sentences because the two answers are two different things.
        """
        with serve(database) as server:
            empty_lookup = server.places("Nowhere").json()
            empty_sweep = done_of(
                events_of(server.search_from(SPRINGFIELDS, difference_limit=0))
            )

        assert empty_lookup["match_count"] == 0
        assert empty_sweep["completed"] is True
        assert empty_sweep["pairs"] == 0
        assert empty_sweep["pairs_compared"] == 1, (
            "it did the work and found nothing, which is not the same as having nothing to do"
        )
        assert differences_of(empty_sweep) == {0: 0}


class TestWhatASweepCosts:
    """AC: "the cost shown before the search runs reflects the reduced candidate set"."""

    def test_the_cost_of_a_sweep_is_the_places_it_can_be_compared_with(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            swept = server.estimate_from(LIMA_PE).json()
            everywhere = server.estimate(0).json()

        assert swept["mode"] == FROM_PLACE_MODE
        assert swept["candidate_pairs"] == 5, (
            "the four other four-letter names and the two other eleven-letter Springfields, less "
            f"itself: its partner set, not every pair in the cache: {swept['candidate_pairs']}"
        )
        assert swept["places"] == 13 and everywhere["places"] == 13, (
            "the same candidate set behind both searches, which is a different number from the one "
            "being compared"
        )
        assert everywhere["candidate_pairs"] > swept["candidate_pairs"]

    def test_the_price_and_the_search_count_the_same_partners(self, database: Path) -> None:
        with serve(database) as server:
            priced = server.estimate_from(LIMA_PE, min_population=25_000).json()
            started = started_of(events_of(server.search_from(LIMA_PE, min_population=25_000)))

        assert priced["candidate_pairs"] == started["candidate_pairs"] == 2
        assert priced["differences"] == started["differences"], (
            "the values a reader is about to see fill up are on the price as well as the stream"
        )
        assert differences_of(priced) == {0: 0, 1: 0, 2: 0, 3: 0}

    def test_the_floor_applies_to_the_partners_and_not_to_the_place_chosen(
        self, database: Path
    ) -> None:
        """A reader can search from a small place: the floor is about what it is compared with."""
        with serve(database) as server:
            started = started_of(
                events_of(server.search_from(LIMA_CA, difference_limit=0, min_population=25_000))
            )

        assert started["origin"]["geonameid"] == LIMA_CA
        assert started["population_floor"] == 25_000
        assert started["candidate_pairs"] == 3, (
            "two candidates of four letters clear the floor besides itself, and the 3,000 person "
            "place it is searching from is not one of them"
        )


class TestRefusingASweep:
    """Nothing here opens a stream, so nothing here can leave a search running."""

    def test_a_place_id_that_is_not_a_place_is_a_404_with_nothing_running(
        self, database: Path
    ) -> None:
        """AC: choosing a place that isn't there is an error, not a blank search."""
        with serve(database) as server:
            answer = server.search_from(424_242)
            health = server.get("/healthz").json()

        assert answer.status_code == 404, answer.text
        body = answer.json()
        assert body["error"] == "PlaceNotFound"
        assert "424242" in body["message"]
        assert "look the name up" in body["message"], (
            f"and where to get an id that exists: {body['message']}"
        )
        assert health["searches"]["live"] == 0

    def test_a_place_id_that_is_not_a_number_is_refused_by_the_request(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.get("/v1/search", from_place="Lima")

        assert answer.status_code == 422, answer.text

    def test_a_search_cannot_ask_for_both_modes_at_once(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.search(1, from_place=LIMA_PE)
            live = server.get("/healthz").json()["searches"]["live"]

        assert answer.status_code == 422, answer.text
        body = answer.json()
        assert body["error"] == "InvalidSearchRequest"
        assert "range" in body["message"] and "threshold" in body["message"], (
            f"the refusal has to say which two things are in each other's way: {body['message']}"
        )
        assert live == 0, "a refused search leaves nothing running"

    def test_a_search_that_names_neither_mode_says_what_it_needs(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.search(None)

        assert answer.status_code == 422, answer.text
        body = answer.json()
        assert body["error"] == "InvalidSearchRequest"
        assert "n" in body["message"] and "from_place" in body["message"]

    def test_a_range_without_a_place_to_sweep_from_is_refused(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.search(1, difference_limit=4)

        assert answer.status_code == 422, answer.text
        assert "belongs to a search from a place" in answer.json()["message"]

    def test_a_range_below_zero_is_refused(self, database: Path) -> None:
        with serve(database) as server:
            answer = server.search_from(LIMA_PE, difference_limit=-1)

        assert answer.status_code == 422, answer.text
        assert answer.json()["error"] == "InvalidSearchRequest"

    def test_a_sweep_toggle_in_everywhere_mode_is_refused_rather_than_ignored(
        self, database: Path
    ) -> None:
        """A parameter nothing is listening to is refused, not swallowed.

        Leaving identical names out only means something to a range that starts at zero: everywhere
        mode reports the pairs its one threshold has, whatever their names do. A caller who sent
        this and got a normal search back would read identical names into the answer they had just
        asked to be left out.
        """
        with serve(database) as server:
            answer = server.search(0, exclude_identical_names=True)

        assert answer.status_code == 422, answer.text
        assert "belongs to a search from a place" in answer.json()["message"]

    def test_a_sweep_cannot_ask_to_ignore_the_equal_length_rule(self, database: Path) -> None:
        """Everywhere mode can turn the prefilter off; a sweep has no comparison without it.

        Two names of different length have no character difference, so a sweep that ignored the
        rule would compare `Lima` with `Springfield` and call the work it did a search.
        """
        with serve(database) as server:
            answer = server.search_from(LIMA_PE, full_comparison=True)

        assert answer.status_code == 422, answer.text
        assert "there is no sweep without that rule" in answer.json()["message"]

    def test_a_priced_sweep_is_refused_for_the_same_reasons_as_a_running_one(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            both = server.estimate(1, from_place=LIMA_PE)
            missing = server.get("/v1/places", name="Lima")

        assert both.status_code == 422, both.text
        assert missing.status_code == 200, missing.text


class TestASweepInTheWay:
    """A sweep holds the one search this server runs, and is described in a sweep's own words.

    A sweep of one name-length group is over in tens of milliseconds, so these do not race a stream
    the way the tests for a three-million-pair scan can. One takes the words off a sweep that really
    ran; the other holds the server with a sweep that was claimed and never stepped, which is the
    only way to be sure the refusal is answering a sweep rather than answering a race. That a
    sweep obeys a hang-up is the runner's business and covered there; that a sweep *stops* when it
    is cancelled is covered at the seam, where the walk is.
    """

    def test_a_sweep_is_described_by_the_place_and_the_range_never_by_a_threshold(
        self, database: Path
    ) -> None:
        with serve(database) as server:
            events = events_of(server.search_from(LIMA_PE, difference_limit=2))
            record = server.get(f"/v1/searches/{started_of(events)['search_id']}").json()

        words = asking_words(record["request"])
        assert "0 to 2 differing characters" in words, words
        assert "Lima" in words and "PE" in words, (
            f"the refusal a reader gets has to name the place they chose: {words}"
        )
        assert "None" not in words and "n=" not in words, (
            f"a sweep never had a threshold, so its words must not quote one: {words}"
        )

    def test_a_sweep_holds_the_server_and_says_which_place_it_is_waiting_on(
        self, database: Path
    ) -> None:
        """AC: one search at a time still means one, now that a search can be a sweep.

        The held sweep carries the echo the endpoint itself built for a sweep on this database,
        taken off the record of one that already finished: what is being asserted is the answer a
        second reader gets, in the words the server writes, not a message assembled by the test.
        """
        with serve(database) as server:
            finished = events_of(server.search_from(LIMA_PE))
            echo = server.get(f"/v1/searches/{started_of(finished)['search_id']}").json()[
                "request"
            ]
            waiting = open_search(SearchRequest(from_place=LIMA_US, database=database))
            record, running = server.registry.claim(waiting, dict(echo))
            assert record is not None and running is None
            try:
                refused = server.search(1)
                assert refused.status_code == 409, refused.text
                body = refused.json()
                assert body["error"] == "SearchAlreadyRunning"
                assert body["search_id"] == record.search_id
                assert body["running"]["request"]["mode"] == FROM_PLACE_MODE
                words = asking_words(body["running"]["request"])
                assert words in body["message"], (
                    f"the refusal has to say what is running in the words the API has for it: "
                    f"{body['message']}"
                )
                assert "0 to 3 differing characters" in words and "Lima" in words
                assert "None" not in body["message"], body["message"]
                assert server.get("/healthz").json()["searches"]["live"] == 1
            finally:
                waiting.control.cancel("the test stopped holding the server open")
                server.registry.finish(record)
                waiting.close()


@pytest.fixture(scope="module")
def real_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`cities1000.txt` imported once, for the timings the ticket asks for.

    146,782 places: the biggest same-name group in it is 45 places and the widest name-length group
    is 19,793, which is the most a sweep can be asked to walk on this cache.
    """
    from support import import_dump

    path = tmp_path_factory.mktemp("real-places") / "places.sqlite3"
    connection = import_dump(path, CITIES_1000)
    connection.close()
    return path


def timed(action: Any) -> tuple[float, Any]:
    """Seconds an action took, and what it answered."""
    started = time.perf_counter()
    answer = action()
    return time.perf_counter() - started, answer


@pytest.mark.slow
class TestASweepOverTheRealPlaces:
    """AC: "results return in well under a second for a representative single-name search"."""

    def test_a_name_looks_up_and_a_sweep_comes_back_on_a_real_cache(
        self, real_database: Path
    ) -> None:
        with serve(real_database) as server:
            lookup, matches = timed(lambda: server.places("Springfield").json())
            biggest = matches["matches"][0]
            sweep, events = timed(
                lambda: events_of(server.search_from(biggest["geonameid"], difference_limit=3))
            )

        assert matches["match_count"] > 10, (
            f"the real cache is expected to hold a dozen Springsfields: {matches['match_count']}"
        )
        assert lookup < 0.5, f"a lookup is one index seek, and this one took {lookup:.3f}s"
        done = done_of(events)
        assert started_of(events)["origin"]["display_name"] == "Springfield"
        assert differences_of(done)[0] == matches["match_count"] - 1, (
            "every other place holding the name, at zero differing characters, measured from the "
            "one place that was chosen"
        )
        assert sweep < 1.0, f"a representative single-name sweep took {sweep:.3f}s"

    def test_the_widest_name_length_group_in_the_cache_sweeps_inside_a_second(
        self, real_database: Path
    ) -> None:
        """The worst case a sweep can be asked for here: the largest group of equal-length names."""
        import sqlite3

        connection = sqlite3.connect(f"file:{real_database}?mode=ro", uri=True)
        length, partners = connection.execute(
            "SELECT name_length, COUNT(*) AS places FROM place GROUP BY name_length "
            "ORDER BY places DESC LIMIT 1"
        ).fetchone()
        origin = int(
            connection.execute(
                "SELECT geonameid FROM place WHERE name_length = ? ORDER BY population DESC "
                "LIMIT 1",
                (length,),
            ).fetchone()[0]
        )
        connection.close()

        with serve(real_database) as server:
            sweep, events = timed(lambda: events_of(server.search_from(origin)))

        assert partners > 10_000, f"the widest group is only {partners} places: the cache changed"
        done = done_of(events)
        assert done["pairs_compared"] == partners - 1
        assert sweep < 1.0, (
            f"the widest name-length group in the cache ({partners} places) took {sweep:.3f}s "
            "to sweep from end to end"
        )
