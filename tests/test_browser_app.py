"""The browser front end: what it asks the API for, and what it must never do for itself.

There is no browser in this suite, so the contract is held from both ends. From the server:
the page, its scripts, its stylesheet and its coastline are served, and every field the page
renders exists on a real streamed search (`THE_WIRE` below is checked against events, not against
a comment). From the
source: the presentation half of the front end is a module of pure functions, and the marking of
a name is one exported function of the adapter, both of which Node runs directly where it is
installed - so "the differing characters are highlighted" and "a search that found nothing says
so with its parameters" are assertions about output rather than about hope. Where Node is not
installed those tests skip, loudly: the suite itself must stay a
`uv run pytest` away on a machine with only the declared Python dependencies.

ADR 0008 is the reason the front end is plain ES modules rather than a build step, and the
reason the last class below is so strict about what the JavaScript is allowed to contain: the
matching, the ranking and the distances all come from the API, and a second copy of any of them
in the page is a second answer to the same question.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from distnsimilarity.core.matching import differing_position_indexes
from http_support import events_of, serve
from browser_support import (
    APP_JS,
    BANNED,
    MAP_JS,
    MEASURED_ASSETS,
    RESULTS_JS,
    drive_page,
    in_presentation,
    in_the_adapter,
    page_assets,
)
from support import dump_row, place_database

#: Every field the front end reads, by where it reads it from. Kept as data so the two tests
#: below can check it in both directions: the API really sends it, and the page really reads it.
THE_WIRE: dict[str, list[str]] = {
    "the dataset listing": [
        "database",
        "datasets",
        "path",
        "present",
        "places",
        "imported",
        "threshold",
        "label",
        "candidate_pairs",
    ],
    "the estimate": [
        "estimate",
        "seconds",
        "basis",
        "comparisons_per_second",
        "population_floor",
        "requested_dataset",
        "dataset",
        "dataset_label",
        "places",
        "n",
        "candidate_pairs",
    ],
    "a started event": [
        "search_id",
        "dataset",
        "dataset_label",
        "requested_dataset",
        "database",
        "n",
        "direction",
        "top_k",
        "min_population",
        "candidate_pairs",
        "origin",
        "differences",
    ],
    "a progress event": [
        "pairs_compared",
        "share_compared",
        "improvements",
        "elapsed_seconds",
        "estimated_seconds_remaining",
        "differences",
    ],
    "a results event": ["seen_pairs", "results", "differences"],
    "one row of the top-K": [
        "first",
        "second",
        "distance_km",
        "character_difference",
        "differing_positions",
    ],
    "one place in a row": [
        "asciiname",
        "display_name",
        "country_code",
        "population",
        "latitude",
        "longitude",
        "geonameid",
    ],
    "a done event": [
        "completed",
        "cancelled",
        "pairs",
        "error",
        "cancel_reason",
        "retryable",
        "differences",
    ],
    "an error event": ["message"],
}


def corrupt_one_place(database: Path, geonameid: int) -> None:
    """Make one stored row not a place, which is the only way an `error` event ever happens.

    A row that is off the globe is refused where it is read - inside the running scan - so this
    is how a test gets the API to answer with an `error` event rather than a status code, and why
    the contract has to say what such an event carries.
    """
    stored = sqlite3.connect(database)
    stored.execute("UPDATE place SET latitude = 400.0 WHERE geonameid = ?", (geonameid,))
    stored.commit()
    stored.close()


def every_field_on_the_wire(tmp_path: Path) -> set[str]:
    """Every key a client can read: one search, the same search broken, one estimate, one listing.

    The second search of the same database is the one that ends in an `error` event, so the
    fields the page reads on a failure are checked against a real failure rather than asserted.
    """
    database = place_database(
        tmp_path / "places.sqlite3",
        [
            dump_row(1, "Analac", 0.0, 0.0, population=90_000),
            dump_row(2, "Bnalac", 0.0, 1.0, population=80_000),
            dump_row(3, "Cnalac", 1.0, 2.0, population=70_000),
        ],
    )
    seen: set[str] = set()

    def keys_of(payload: object) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                seen.add(str(key))
                keys_of(value)
        elif isinstance(payload, list):
            for value in payload:
                keys_of(value)

    with serve(database) as server:
        keys_of(server.get("/v1/datasets").json())
        keys_of(server.estimate(1).json())
        for event in events_of(server.search(1)):
            keys_of(event)
        corrupt_one_place(database, 2)
        for event in events_of(server.search(1)):
            keys_of(event)
    return seen




class TestThePageIsServed:
    """`distnsimilarity-serve` serves the app, so there is one command and one port."""

    def test_the_root_of_the_api_is_the_page(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3", [dump_row(1, "Aa", 0.0, 0.0, population=1_000)]
        )

        with serve(database) as server:
            response = server.get("/")

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/html"), (
            f"the root is a page, got {response.headers['content-type']!r}"
        )
        assert "<!doctype html>" in response.text.lower()

    def test_the_page_asks_which_places_the_server_is_searching(self, tmp_path: Path) -> None:
        """The page cannot know the place count or the cache path; the server does."""
        database = place_database(
            tmp_path / "places.sqlite3", [dump_row(1, "Aa", 0.0, 0.0, population=1_000)]
        )

        with serve(database) as server:
            response = server.get("/")

        assert "places" in response.text
        assert 'id="database-line"' in response.text, (
            "somewhere on the page says what is being searched over, before anything is searched"
        )

    def test_the_script_and_the_stylesheet_are_served_from_here(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3", [dump_row(1, "Aa", 0.0, 0.0, population=1_000)]
        )

        with serve(database) as server:
            script = server.get("/static/app.js")
            presentation = server.get("/static/results.js")
            styles = server.get("/static/styles.css")

        assert script.status_code == 200, script.text[:200]
        assert "javascript" in script.headers["content-type"], script.headers["content-type"]
        assert presentation.status_code == 200, presentation.text[:200]
        assert "javascript" in presentation.headers["content-type"]
        assert styles.status_code == 200
        assert "text/css" in styles.headers["content-type"]

    def test_the_page_is_written_in_modules_so_a_build_step_stays_optional(self) -> None:
        assets = page_assets()
        assert 'type="module"' in assets["index.html"], (
            "the front end is ES modules loaded straight from this server: no bundler, and "
            "nothing to remember to run before the page is right"
        )
        assert "import {" in assets["app.js"], (
            "the DOM wiring and the presentation are separate files, so the presentation can be "
            "run outside a browser"
        )

    def test_nothing_the_page_loads_comes_from_another_machine(self) -> None:
        """A tool that runs on one laptop does not fetch its behaviour from somebody else's."""
        for name, text in page_assets().items():
            offenders = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', text)
            assert offenders == [], f"{name} loads something from elsewhere: {offenders}"


class TestTheControls:
    """Every parameter a search takes is on the page, and starting one is not a page load."""

    def test_every_parameter_of_a_search_is_on_the_page(self) -> None:
        assets = page_assets()
        page = assets["index.html"]
        for name in ("n", "dataset", "min_population", "direction", "top_k"):
            assert f'name="{name}"' in page, f"{name} is a search parameter and is not on the page"
        assert '<form id="search-form"' in page, "the controls are one form, so one submit starts it"

    def test_the_direction_offers_both_ends_of_the_range_in_words(self) -> None:
        page = page_assets()["index.html"]
        assert 'value="greatest"' in page and 'value="least"' in page
        assert "greatest distance first" in page and "least distance first" in page, (
            "the words on an option are the decision; the value is for the API"
        )

    def test_the_page_sends_its_parameters_as_a_search_and_never_reloads(self) -> None:
        script = page_assets()["app.js"]
        assert '"/v1/search"' in script and '"/v1/estimate"' in script and '"/v1/datasets"' in script
        assert "location.reload" not in script and "location.href" not in script, (
            "starting, restating and stopping a search are all done on this page"
        )
        assert "preventDefault" in script, "pressing search must not be a form post"

    def test_the_dataset_field_is_a_choice_filled_from_the_server(self) -> None:
        page = page_assets()["index.html"]
        assert '<select id="dataset"' in page
        assert "cities of 1,000 or more people" not in page, (
            "the dataset choices come from /v1/datasets, which is what this cache can honour; "
            "a list written into the page would be a second, staler answer"
        )

    def test_a_running_search_can_be_stopped_from_the_page(self) -> None:
        script = page_assets()["app.js"]
        assert "AbortController" in script and ".abort()" in script, (
            "hanging up is how this API is told to stop: the server reads a closed connection "
            "as a cancellation"
        )


#: One `/v1/estimate` answer, in the shape the endpoint answers with. The parameters that come back
#: are the ones that went up - the harness substitutes them from the request, as the real endpoint
#: echoes them - so a test cannot pass by reading a stale fixture after changing a parameter. The
#: numbers are one small dataset's: `cities of 5,000 or more people`, 120 places, 7,140 pairs.
COSTED_SEARCH: dict[str, object] = {
    "requested_dataset": "@query",
    "dataset": 5000,
    "dataset_label": "cities of 5,000 or more people",
    "n": "@query",
    "min_population": "@query",
    "equal_length_prefilter": True,
    "database": "/tmp/places.sqlite3",
    "top_k": "@query",
    "direction": "@query",
    "places": 120,
    "population_floor": 5000,
    "candidate_pairs": 7140,
    "estimate": {
        "seconds": 0.4,
        "basis": "measured on this machine",
        "comparisons_per_second": 18_000_000,
    },
}


def query_pairs(queries: list[str]) -> list[dict[str, str]]:
    """`["n=3&dataset=5000", ...]` as mappings, so a test says which parameter changed."""
    return [dict(one.split("=", 1) for one in query.split("&") if one) for query in queries]


def parameters_of(script: str) -> str:
    """The body of `parametersToSend`, which is where the page decides what a search is asked for."""
    body = script[script.index("function parametersToSend(") :]
    return body[: body.index("\n}")]


#: The controls that say what state a search is in. Read the same way mid-search and after one, so
#: the two states can be compared: one search runs at a time, so a page mid-search offers Start and
#: takes away Stop, and the fields stay editable either way because setting up the next search while
#: watching this one is not a second search.
CONTROLS = ("searching", "startDisabled", "stopHidden", "indicatorHidden", "fieldsDisabled")


def control_state(controls: dict[str, object]) -> dict[str, object]:
    """The parts of the page's controls that carry the search state, ignoring its words."""
    return {name: controls[name] for name in CONTROLS}


#: Four events, quoted from a real `n=2 dataset=1000` scan against the 146,782-place cache (its
#: newline-delimited JSON, with the rows cut to the pair this file draws). They are literals rather
#: than something built here because the point of the control tests is what the page does while a
#: search is mid-flight - and a page fed the events a real server really sent is being asked about
#: the search it would really be watching. `results` carries no `candidate_pairs`: only a tick knows
#: the denominator, which is a fact the wording has to respect.
STARTED: dict[str, object] = {
    "type": "started",
    "search_id": "4a4c2bc8bf96",
    "requested_dataset": "1000",
    "dataset": 1000,
    "dataset_label": "cities of 1,000 or more people",
    "n": 2,
    "min_population": None,
    "equal_length_prefilter": True,
    "database": "/tmp/distn-smoke/places.sqlite3",
    "top_k": 10,
    "direction": "greatest",
    "places": 124272,
    "population_floor": 1000,
    "candidate_pairs": 666215784,
    "improvements": 0,
    "pairs": 0,
    "elapsed_seconds": 0.0,
    "estimate": {
        "candidate_pairs": 666215784,
        "comparisons_per_second": None,
        "seconds": None,
        "basis": "not measured on this machine yet",
    },
}

TICK: dict[str, object] = {
    "type": "progress",
    "search_id": "4a4c2bc8bf96",
    "places": 124272,
    "pairs_compared": 41_000_000,
    "candidate_pairs": 666215784,
    "share_compared": 0.0615,
    "pairs": 10,
    "improvements": 3,
    "elapsed_seconds": 20.4,
    "estimated_seconds_remaining": 312.0,
}

#: The leading find of that scan: two Spanish-named places on the far side of each other, whose arc
#: crosses the antimeridian.
ARANGA_AND_ARANUI: dict[str, object] = {
    "first": {
        "geonameid": 2520736,
        "asciiname": "Aranga",
        "display_name": "Aranga",
        "latitude": 43.23469,
        "longitude": -8.01705,
        "population": 970,
        "country_code": "ES",
    },
    "second": {
        "geonameid": 2194088,
        "asciiname": "Aranui",
        "display_name": "Aranui",
        "latitude": -43.50833,
        "longitude": 172.70013,
        "population": 940,
        "country_code": "NZ",
    },
    "distance_km": 19949.645,
    "character_difference": 2,
    "differing_positions": [0, 3],
}


def a_list(rows: list[dict[str, object]], *, improvements: int = 1, seen_pairs: int = 1) -> dict[str, object]:
    """A `results` event carrying the whole best-so-far list, as the API sends it."""
    return {
        "type": "results",
        "search_id": STARTED["search_id"],
        "direction": "greatest",
        "top_k": 10,
        "results": rows,
        "improvements": improvements,
        "seen_pairs": seen_pairs,
        "pairs_compared": TICK["pairs_compared"],
        "elapsed_seconds": TICK["elapsed_seconds"],
    }


FINISHED: dict[str, object] = {
    "type": "done",
    "search_id": STARTED["search_id"],
    "places": 124272,
    "population_floor": 1000,
    "dataset": 1000,
    "dataset_label": "cities of 1,000 or more people",
    "database": "/tmp/distn-smoke/places.sqlite3",
    "candidate_pairs": 666215784,
    "pairs_compared": 41_000_000,
    "pairs": 10,
    "improvements": 3,
    "comparison_polls": 61_000,
    "elapsed_seconds": 20.4,
    "comparisons_per_second": 2_009_803.9,
    "share_compared": 0.0615,
    "completed": True,
    "cancelled": False,
    "cancel_reason": None,
    "retryable": False,
    "error": None,
}

#: One search, streamed: the price, `started`, one tick, one list, then a stream that stays open
#: until the test hangs up on it or releases it.
ONE_SEARCH_OPEN = [
    [[STARTED], [TICK], [a_list([ARANGA_AND_ARANUI], improvements=3, seen_pairs=10)], "hold"]
]


def one_search(**echoed: object) -> list[list[object]]:
    """One whole search, streamed to its end, with the `started` echoing these parameters.

    The echo is the only part that varies: the point of passing `n=4` is that the answer comes back
    saying `n=4`, so a test about searching again cannot pass by drawing one search's echo twice.
    """
    return [
        [{**STARTED, **echoed}],
        [TICK],
        [a_list([ARANGA_AND_ARANUI], improvements=3, seen_pairs=10)],
        [FINISHED],
    ]


#: A stream with a `done` still to come: a search stopped while the server had more to say, which
#: is the only shape that can catch a page painting a finished search it had already given up on.
OPEN_THEN_FINISHED = [ONE_SEARCH_OPEN[0] + [[FINISHED]]]


class TestDecidingWhatASearchWillCost:
    """The price is what a person decides with, so it has to be on screen before the decision."""

    def test_the_price_of_the_parameters_on_screen_is_shown_before_anything_is_searched(self) -> None:
        seen = drive_page(
            """
            await page.settle();
            return {
              line: page.lines().estimate,
              costed: page.askedFor("/v1/estimate"),
              searched: page.askedFor("/v1/search"),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            parameters={"n": 3, "dataset": "5000", "direction": "greatest", "top_k": 50},
        )

        assert seen["searched"] == [], "a price is not a reason to start a search"
        assert seen["costed"] != [], "the page should ask what the parameters on screen would cost"
        line = seen["line"]
        assert "3 differing characters" in line, (
            f"the number of differing characters is one of the things being decided, so it belongs "
            f"in the sentence about what it costs: {line}"
        )
        assert "population floor of 5,000" in line, (
            f"the floor in effect is the other half of the decision, and it is the server's resolved "
            f"floor rather than whatever box was left blank: {line}"
        )
        assert "7,140" in line

    def test_changing_a_parameter_asks_the_server_what_that_one_costs(self) -> None:
        seen = drive_page(
            """
            await page.settle();
            const before = page.lines().estimate;
            page.parameters({ n: 1 });
            await page.settle();
            return {
              before,
              after: page.lines().estimate,
              costed: page.askedFor("/v1/estimate"),
              searched: page.askedFor("/v1/search"),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            parameters={"n": 3, "dataset": "5000", "direction": "greatest", "top_k": 50},
        )

        assert "3 differing characters" in seen["before"]
        assert "1 differing character" in seen["after"], (
            f"the price on screen must be the price of the search about to run, not of the one "
            f"before it: {seen['after']}"
        )
        assert [sent.get("n") for sent in query_pairs(seen["costed"])] == ["3", "1"], (
            f"one price per distinct set of parameters, the second one for the change: {seen['costed']}"
        )
        assert seen["searched"] == []


class TestOneSearchAtATimeOnThePage:
    """One search per machine, which is what a page must remember for itself.

    The server refuses a second search (`tests/test_http_search.py` holds that), but a page that
    hung up on its own search in order to start another was never refused anything: it simply
    threw away a search the reader was watching. The ticket asks for a refusal instead, in words,
    with the way out in the same sentence - and the way out is Stop, which is already on screen.
    """

    def test_pressing_search_while_one_runs_is_refused_in_words_that_say_what_to_do(self) -> None:
        seen = drive_page(
            """
            await page.settle();
            page.start();
            await page.settle();
            await page.start();
            await page.settle();
            return {
              lines: page.lines(),
              controls: page.controls(),
              searches: page.askedFor("/v1/search"),
              costed: page.askedFor("/v1/estimate").length,
              rows: page.rows().length,
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=ONE_SEARCH_OPEN,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert seen["searches"] != [], "the first press has to have started a search"
        assert len(seen["searches"]) == 1, (
            f"a second press must not ask for a second search: {seen['searches']}"
        )
        message = seen["lines"]["message"]
        assert "already running" in message, message
        assert str(STARTED["search_id"]) in message, (
            f"the refusal has to name the search in the way, as the server's does: {message}"
        )
        assert "cities of 1,000 or more people" in message, message
        assert "2 differing characters" in message, message
        assert "stop" in message.lower(), (
            f"a refusal that does not say what to do about it is a dead end: {message}"
        )
        assert seen["rows"] == 1, "refusing the second search must leave the first one's list alone"
        assert seen["controls"]["startDisabled"] is True, (
            "the control that was refused says so on its own, rather than looking pressable"
        )
        assert seen["controls"]["stopHidden"] is False
        assert "Searching" in seen["lines"]["status"], (
            f"the running search keeps its own status line: {seen['lines']['status']}"
        )


class TestReadingTheStreamInPieces:
    """The bytes of a search do not arrive as records, and the page may not pretend they do.

    `parseStream` takes the unfinished tail of one read and hands it to the next. This is the shape
    the bug that #10 was handed over with lived in: a page that throws away a read whose last record
    is incomplete draws nothing at all, and a page fed one whole record per chunk never asks the
    question. So the records here are cut in half, at a different place in each, and what is asked is
    what ended up on screen.
    """

    def test_a_record_cut_across_two_reads_is_drawn_once_and_not_reported_as_a_failure(self) -> None:
        started = json.dumps(STARTED)
        listing = json.dumps(a_list([ARANGA_AND_ARANUI], improvements=3, seen_pairs=10))
        # Cut in two different places, so neither half is accidentally a whole record: the first read
        # ends inside the `started`, the second inside the list.
        halves = [started[: len(started) // 2], started[len(started) // 2 :] + "\n"]
        halves += [listing[:40], listing[40:] + "\n"]
        seen = drive_page(
            """
            page.start();
            await page.settle();
            return {
              status: page.lines().status,
              rows: page.rows(),
              message: page.lines().message,
              statuses: page.wrote("status-text"),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[[*halves, "hold"]],
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert seen["status"].startswith("Searching"), (
            f"the half a reader got first is still the start of a search: {seen['status']}"
        )
        assert len(seen["rows"]) == 1, (
            f"a record that arrived in two pieces is one row, not none and not two: {seen['rows']}"
        )
        assert "Aranga" in seen["rows"][0] and "19,949.645" in seen["rows"][0], seen["rows"][0]
        assert len(seen["statuses"]) == 2 and len(set(seen["statuses"])) == 2, (
            "each record is drawn once when its last byte arrives - once per record, not once per "
            f"read and not twice for the piece that was finished: {seen['statuses']}"
        )
        assert seen["statuses"][0].startswith("Searching cities of 1,000"), seen["statuses"]
        assert seen["status"] == seen["statuses"][-1], seen["status"]
        assert seen["message"] == "", (
            f"a search whose bytes are merely slow is not a search that failed: {seen['message']}"
        )


class TestStoppingARunningSearch:
    """A search is under the reader's control while it runs, which is what Stop is for.

    Hanging up the connection is the only way this API is told to stop, so these are assertions
    about the connection the page let go of, not about a spinner disappearing: a stream still being
    read is a scan still running on the server.
    """

    def test_pressing_stop_hangs_up_on_the_stream_and_gives_the_controls_back(self) -> None:
        seen = drive_page(
            """
            page.start();
            await page.settle();
            page.stop();
            await page.settle();
            return {
              searches: page.searches(),
              controls: page.controls(),
              lines: page.lines(),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=ONE_SEARCH_OPEN,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["searches"]) == 1, seen["searches"]
        assert seen["searches"][0]["aborted"] is True, (
            "stopping is closing the connection: that is how this server learns nobody is watching, "
            f"and a scan left reading is a scan still costing the machine: {seen['searches']}"
        )
        controls = seen["controls"]
        assert control_state(controls) == {
            "searching": False,
            "startDisabled": False,
            "stopHidden": True,
            "indicatorHidden": True,
            "fieldsDisabled": False,
        }, f"a stopped search leaves a usable page, not a page stuck mid-search: {controls}"
        assert seen["lines"]["status"] == "Stopped", seen["lines"]
        assert str(STARTED["search_id"]) in seen["lines"]["message"], (
            f"the sentence says which search ended, so a reader can tie it to the scan they watched: "
            f"{seen['lines']['message']}"
        )

    def test_a_stopped_search_keeps_the_list_it_had_found_and_says_so(self) -> None:
        seen = drive_page(
            """
            page.start();
            await page.settle();
            page.stop();
            await page.settle();
            return {
              rows: page.rows(),
              tableHidden: page.tableHidden(),
              message: page.lines().message,
              caption: page.lines().caption,
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=ONE_SEARCH_OPEN,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["rows"]) == 1, (
            f"stopping early is how a person reads a scan that is already good enough, so the "
            f"finding has to survive it: {seen['rows']}"
        )
        row = seen["rows"][0]
        assert "Aranga" in row and "Aranui" in row and "19,949.645" in row, (
            f"and it has to still be readable, not a row of empty cells: {row}"
        )
        assert seen["tableHidden"] is False
        message = seen["message"]
        assert "list on screen is what it had found" in message, message
        assert "nothing more is coming" in message, (
            f"a list that has stopped moving has to be said to have stopped: {message}"
        )
        assert "Aranga" in seen["caption"], (
            f"the map keeps the pair the search had found too: {seen['caption']}"
        )

    def test_a_stopped_search_never_reports_itself_as_finished(self) -> None:
        """AC: a stopped search does not emit a terminal success state as though it had finished.

        The server may still be writing when the page hangs up, and a page that read a `done` after
        a Stop would be claiming a scan had walked all its candidates when it had been cancelled
        part way. So this stops mid-stream, then lets the rest of the stream arrive, and asks the
        whole history of the two status lines rather than whatever is on screen at the end.
        """
        seen = drive_page(
            """
            page.start();
            await page.settle();
            page.stop();
            await page.settle();
            page.release();
            await page.settle();
            return {
              status: page.wrote("status-text"),
              message: page.wrote("message-line"),
              searches: page.searches(),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=OPEN_THEN_FINISHED,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert seen["searches"][0]["aborted"] is True
        finished = [line for line in seen["status"] + seen["message"] if "finish" in line.lower()]
        assert finished == [], (
            f"a search this page stopped must never be described as finished: {seen}"
        )
        assert seen["status"][-1] == "Stopped", seen["status"]
        assert [line for line in seen["message"] if "Stopped" in line] != [], seen["message"]

    def test_pressing_the_pages_own_stop_button_hangs_up_on_the_stream(self) -> None:
        """The control a reader presses, not the function the module exports.

        `wireUpPage` is what makes Stop mean Stop, and it is the one part of the page a driven test
        can otherwise break without anything failing: a listener that is not attached, or attached to
        the wrong control, is a Stop button that does nothing while the scan carries on costing.
        """
        seen = drive_page(
            """
            page.press("start-button");
            await page.settle();
            page.press("stop-button");
            await page.settle();
            return {
              searches: page.searches(),
              status: page.lines().status,
              controls: page.controls(),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=ONE_SEARCH_OPEN,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["searches"]) == 1, seen["searches"]
        assert seen["searches"][0]["aborted"] is True, (
            f"the button on the page is what ends the scan, so it is the button that has to abort: "
            f"{seen['searches']}"
        )
        assert seen["status"] == "Stopped", seen["status"]
        assert seen["controls"]["stopHidden"] is True and seen["controls"]["startDisabled"] is False

    def test_pressing_stop_with_nothing_running_says_so_without_undressing_the_last_search(self) -> None:
        """Stop is on screen only while a search runs, but Enter in a field still reaches it.

        A stop that stops nothing must still leave the page honest: the status line belongs to the
        search that finished, and replacing it with a word about idleness would throw away the answer
        that is already there.
        """
        seen = drive_page(
            """
            await page.start();
            const finished = page.lines().status;
            page.stop();
            await page.settle();
            return {
              finished,
              status: page.lines().status,
              message: page.lines().message,
              searches: page.searches(),
              controls: page.controls(),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[one_search()],
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["searches"]) == 1, (
            f"pressing Stop is not a way to start a search, or to start a second one: {seen['searches']}"
        )
        assert "no search running" in seen["message"].lower(), seen["message"]
        assert "finished" in seen["finished"].lower(), (
            f"the search before this press did finish, which is what the status is for: {seen['finished']}"
        )
        assert seen["status"] == seen["finished"], (
            f"a stop that stopped nothing has not changed what the last search said: {seen['status']}"
        )
        assert seen["controls"]["startDisabled"] is False


class TestSearchingAgainWithoutReloading:
    """AC: changing the parameters and restarting needs no page reload.

    A reload would throw away the list, the map and the price with it, and would leave the previous
    stream to be noticed by the server rather than hung up on. Everything here happens to one page.
    """

    def test_stopping_then_pressing_search_again_runs_a_second_search_on_the_same_page(self) -> None:
        seen = drive_page(
            """
            page.start();
            await page.settle();
            page.stop();
            await page.settle();
            const left = page.rows().length;
            page.parameters({ n: 4 });
            await page.settle();
            await page.start();
            await page.settle();
            return {
              left,
              searches: page.searches(),
              urls: page.urls(),
              status: page.wrote("status-text"),
              rows: page.rows(),
              controls: page.controls(),
              reloaded: page.reloaded(),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[ONE_SEARCH_OPEN[0], one_search(n=4)],
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert seen["left"] == 1, "the stopped search left its list on the page"
        assert len(seen["searches"]) == 2, (
            f"a stopped search must not leave the page unable to search again: {seen['searches']}"
        )
        first, second = query_pairs([one["query"] for one in seen["searches"]])
        assert first["n"] == "2" and second["n"] == "4", (
            f"the second search carries the parameters that are on the page now: {seen['searches']}"
        )
        assert seen["reloaded"] is False, "the page never reloaded: this is the same page"
        assert seen["status"][-1] == "Search finished", seen["status"]
        assert len(seen["rows"]) == 1
        assert seen["controls"]["startDisabled"] is False

    def test_the_price_of_the_second_search_is_asked_for_before_its_stream_opens(self) -> None:
        seen = drive_page(
            """
            page.parameters({ n: 4 });
            await page.settle();
            await page.start();
            await page.settle();
            return {
              urls: page.urls(),
              costed: page.askedFor("/v1/estimate"),
              searches: page.searches(),
              estimate: page.lines().estimate,
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[one_search(n=4)],
            parameters={"n": 2, "dataset": "5000", "direction": "greatest", "top_k": 50},
        )

        costed = seen["urls"].index("/v1/estimate?n=4&dataset=5000&direction=greatest&top_k=50")
        searched = seen["urls"].index("/v1/search?n=4&dataset=5000&direction=greatest&top_k=50")
        assert costed < searched, (
            f"the price of the search has to be on screen before its stream opens: {seen['urls']}"
        )
        assert len(set(seen["costed"])) == len(seen["costed"]), (
            f"counting the same candidate pairs twice for one decision is the expensive part of "
            f"the page doing that: {seen['costed']}"
        )
        assert "4 differing characters" in seen["estimate"], seen["estimate"]


class TestRapidClicking:
    """AC: repeated rapid start and stop clicks cannot leave an orphaned scan running.

    The page can only speak for the page: every stream it opened has to be one it either watched to
    the end or hung up on. `tests/test_http_search.py` holds the other half - that a hung-up stream
    really does stop the scan behind it - and the two together are what 'no orphans' means.
    """

    def test_alternating_start_and_stop_leaves_every_stream_the_page_opened_hung_up_on(self) -> None:
        seen = drive_page(
            """
            for (let press = 0; press < 5; press += 1) {
              page.start();
              await page.settle(2);
              page.stop();
              await page.settle(2);
            }
            await page.settle();
            return { searches: page.searches(), controls: page.controls(), lines: page.lines() };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[ONE_SEARCH_OPEN[0]] * 5,
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["searches"]) == 5, seen["searches"]
        still_reading = [one for one in seen["searches"] if not one["aborted"]]
        assert still_reading == [], (
            f"a stream this page stopped reading but did not hang up on is an orphaned scan: "
            f"{still_reading}"
        )
        assert seen["controls"]["searching"] is False
        assert seen["controls"]["startDisabled"] is False
        assert seen["lines"]["status"] == "Stopped", seen["lines"]

    def test_pressing_search_repeatedly_while_a_search_runs_starts_one_search_only(self) -> None:
        seen = drive_page(
            """
            page.start();
            await page.settle();
            for (let press = 0; press < 4; press += 1) {
              await page.start();
            }
            await page.settle();
            return {
              searches: page.searches(),
              rows: page.rows().length,
              status: page.wrote("status-text"),
              controls: page.controls(),
              refused: page.wrote("message-line").filter((line) => line !== ""),
            };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[ONE_SEARCH_OPEN[0]],
            parameters={"n": 2, "dataset": "1000", "direction": "greatest", "top_k": 10},
        )

        assert len(seen["searches"]) == 1, seen["searches"]
        assert seen["rows"] == 1, "and the running search keeps drawing while the presses are refused"
        assert seen["status"] != [] and all(
            line.startswith("Searching") for line in seen["status"]
        ), f"refusing a search must not disturb the status of the one running: {seen['status']}"
        assert len(set(seen["refused"])) == 1 and len(seen["refused"]) == 4, seen["refused"]
        assert control_state(seen["controls"]) == {
            "searching": True,
            "startDisabled": True,
            "stopHidden": False,
            "indicatorHidden": False,
            "fieldsDisabled": False,
        }, (
            "mid-search the page says what it is doing: Stop is offered, Start is refused twice over "
            f"- by the control and by the sentence - and the fields stay open to set up the next one: "
            f"{seen['controls']}"
        )
        assert "already running" in seen["refused"][0], (
            f"the refusal has to say what is in the way and what to do about it: {seen['refused'][0]}"
        )
        assert "Stop" in seen["refused"][0] and "4a4c2bc8bf96" in seen["refused"][0], (
            f"and name the way out and the search it is refusing to interrupt: {seen['refused'][0]}"
        )


class TestTheWireThePageReads:
    def test_every_field_the_page_reads_is_on_the_wire(self, tmp_path: Path) -> None:
        available = every_field_on_the_wire(tmp_path)
        for where, names in THE_WIRE.items():
            missing = sorted(name for name in names if name not in available)
            assert missing == [], f"{where}: the page reads fields the API never sends: {missing}"

    def test_the_page_reads_every_field_the_contract_lists(self) -> None:
        assets = page_assets()
        text = assets["app.js"] + assets["results.js"]
        for where, names in THE_WIRE.items():
            missing = sorted(name for name in names if f'"{name}"' not in text and f".{name}" not in text)
            assert missing == [], f"{where}: named in the contract, never read by the page: {missing}"

    def test_the_page_handles_every_kind_of_event_the_server_sends(self) -> None:
        text = page_assets()["app.js"]
        for kind in ("started", "progress", "results", "done", "error"):
            assert f'"{kind}"' in text, f"a stream that sent `{kind}` would leave the page waiting"

    def test_the_page_has_an_answer_for_a_refusal_as_well_as_a_stream(self) -> None:
        """Every refusal this server makes has to arrive as a sentence, not a spinner."""
        text = page_assets()["app.js"]
        assert "ok" in text and "json" in text.lower(), (
            "a non-200 answer carries a JSON `message`; the page reads it rather than assuming 200"
        )
        assert "catch" in text, "a search that fails has to be caught, not left half-rendered"

    def test_the_fields_on_the_form_are_the_parameters_on_the_wire(self) -> None:
        """The field names become the query string, so they are the APIs names and no others.

        `source` is the one that has to be absent: #12 refused it, and a page still asking which
        source to read would be this servers worst client. `csv` belongs to #11.
        """
        page = page_assets()["index.html"]
        sent = sorted(re.findall(r'<(?:input|select)\b[^>]*\bname="([^"]+)"', page))
        assert sent == [
            "dataset",
            "difference_limit",
            "direction",
            "exclude_identical_names",
            "from_place",
            "min_population",
            "n",
            "top_k",
        ], (
            f"these names go out as query parameters: {sent}"
        )
        script = page_assets()["app.js"]
        sent = parameters_of(script)
        assert "new FormData(form)" in sent, (
            "the form is forwarded as it stands: every named field on it and nothing invented, "
            f"so a field added to the page is a parameter on the wire without a new line of script: {sent}"
        )
        assert "${ESTIMATE_ENDPOINT}?" in script and "${SEARCH_ENDPOINT}?" in script, (
            "one set of parameters pays for the estimate and runs the search, so the price cannot "
            "be quoted for a different search from the one that starts"
        )
        for invented in ("source", "csv", "sort", "offset"):
            assert f'"{invented}"' not in script and f'name="{invented}"' not in page, (
                f"the page sends {invented}, which this API does not take"
            )

    def test_a_control_left_empty_sends_nothing_rather_than_a_blank_value(self) -> None:
        """A population floor nobody typed is the absence of a rule, not a rule about nothing.

        `min_population=` is not "no floor", it is a request the endpoint refuses over a field no
        ever filled in, so the parameter has to be absent from the query the server is handed.
        """
        seen = drive_page(
            """
            page.parameters({ min_population: "" });
            await page.settle();
            await page.start();
            await page.settle();
            return { search: page.searches()[0].query, costed: page.askedFor("/v1/estimate").at(-1) };
            """,
            json_bodies={"/v1/estimate": COSTED_SEARCH},
            streams=[one_search()],
            parameters={
                "n": 2,
                "dataset": "1000",
                "direction": "greatest",
                "top_k": 10,
                "min_population": "1000",
            },
        )

        sent = parse_qs(seen["search"])
        assert "min_population" not in sent, (
            f"the empty control arrived as a parameter the endpoint would refuse: {sent}"
        )
        assert sent["n"] == ["2"] and sent["dataset"] == ["1000"], (
            f"and leaving one field out must not take the others with it: {sent}"
        )
        assert "min_population" not in seen["costed"], (
            f"the price has to be for the search that runs, empty control and all: {seen['costed']}"
        )
        script = page_assets()["app.js"]
        sent = parameters_of(script)
        assert 'text !== ""' in sent, (
            "the filter that does this is one line, shared by every control on the form"
        )
        assert "new URLSearchParams()" in sent, (
            "the parameters are built once, so the estimate and the search cannot disagree about "
            "which search the price was for"
        )

    def test_every_element_the_script_reaches_for_is_in_the_page(self) -> None:
        page = page_assets()["index.html"]
        script = page_assets()["app.js"]
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
        assert wanted, "the script is expected to drive the page by element id"
        absent = sorted(name for name in wanted if f'id="{name}"' not in page)
        assert absent == [], f"the script looks for elements the page never made: {absent}"


class TestThePageWhileTheSearchRuns:
    """The acceptance criteria that are about *when* something appears, not what it says.

    These are read off the adapter's own source, because the behaviour is a question of order -
    price before stream, line before line, one tbody kept and refilled - and an order is exactly
    what a test without a browser can still hold a file to.
    """

    def test_the_price_is_asked_for_and_shown_before_the_search_is_committed_to(self) -> None:
        script = page_assets()["app.js"]
        starting = script[script.index("export async function start(") :]
        starting = starting[: starting.index("\n}")]
        assert "await showCost(sent)" in starting, (
            "starting a search puts a price on the page as part of starting it, rather than "
            "opening the stream and quoting afterwards"
        )
        assert starting.index("await showCost(sent)") < starting.index("await watch("), (
            "inside starting a search, the cost is on screen before the stream is opened: "
            "afterwards it is a receipt rather than a decision"
        )
        assert "const sent = String(parametersToSend());" in starting and "${SEARCH_ENDPOINT}?${sent}" in starting, (
            "one parameter string is what the price was asked for and what the stream is opened with: "
            "the form stays editable while a search runs, so reading it twice could quote one search "
            "and start another"
        )
        quoting = script[script.index("async function quoteCost(") :]
        quoting = quoting[: quoting.index("\n}")]
        assert "${ESTIMATE_ENDPOINT}?" in quoting, (
            "and the line it waits for is the server's own answer to that exact parameter string"
        )
        assert "estimateLine.textContent = costWords(" in quoting, (
            "the answer is put on the page by the presentation module, not assembled in the adapter"
        )

    def test_the_answer_is_read_as_a_stream_rather_than_waited_for(self) -> None:
        """A list that assembles during the scan needs a client that reads during it too."""
        script = page_assets()["app.js"]
        assert "body.getReader()" in script and "await reader.read()" in script, (
            "the body is read chunk by chunk; waiting for the whole body would draw the list "
            "once, at the end, which is the thing this page exists to avoid"
        )
        assert "response.text()" not in script, (
            "reading a search to a string is reading it after it has finished"
        )
        assert "handle(event)" in script, "each line is drawn before the next read is awaited"

    def test_the_list_is_refilled_in_place_rather_than_rebuilt_or_reloaded(self) -> None:
        script = page_assets()["app.js"]
        assert "resultRows.replaceChildren(" in script, (
            "the same tbody is filled again, so the list tightens where it is"
        )
        assert "innerHTML" not in script, (
            "the list is built from elements: a place name is data, never markup to parse"
        )

    def test_the_working_indicator_is_turned_on_and_off_by_the_search(self) -> None:
        page = page_assets()["index.html"]
        script = page_assets()["app.js"]
        assert re.search(r'<span id="working-indicator"[^>]*hidden', page), (
            "the indicator starts hidden: it is not a spinner that happens to be on the page"
        )
        assert "workingIndicator.hidden = !searching" in script
        assert 'document.body.classList.toggle("searching"' in script, (
            "the stylesheet draws the indicator from `body.searching`, so the script has to say "
            "when a search is running rather than leave it looking hung"
        )

    def test_the_working_line_is_worded_by_the_presentation_module(self) -> None:
        """The status says which search is working, and the sentence is not the adapter's.

        The wording is checked in the presentation class; what belongs here is that the adapter
        asks for it, because a status line assembled in the page is a sentence the suite has to
        open a browser to read.
        """
        status = page_assets()["app.js"]
        assert "setStatus(searchingWords(event))" in status, (
            "`started` becomes the working line through the presentation module: a status that "
            "only says \"searching\" says nothing about the search that is running"
        )

    def test_the_search_can_be_hung_up_on_rather_than_watched_to_the_end(self) -> None:
        """AC: stopping is possible, and the list that arrived stays on the screen.

        `AbortController` is the only way a browser hangs up on a body it is reading, and the
        search notices through the same signal the fetch was handed - which is how this API is
        told to cancel.
        """
        script = page_assets()["app.js"]
        assert "new AbortController()" in script
        assert "fetch(url, { signal })" in script, (
            "the controller has to be the one the fetch reads, or hanging up leaves the scan running"
        )
        assert "function abandon()" in script and "controller.abort()" in script
        assert 'stopButton.addEventListener("click", stop)' in script, (
            "the page has a control for hanging up, rather than a reload that hopes for the best"
        )
        assert "if (running !== controller)" in script, (
            "hanging up is not a failure, and must not be reported as one"
        )
        stopped = script[script.index("function stop(event)") :]
        assert "clearRows" not in stopped, (
            "stopping leaves the list that arrived rather than emptying it"
        )

    def test_pressing_search_again_never_hangs_up_on_the_search_being_read(self) -> None:
        """Two streams drawing one list is a page that disagrees with itself about what is running.

        It used to be resolved by hanging up on the first search, which was a cancellation wearing
        a restart's clothes: the reader lost a list without being told. The page refuses instead,
        so what belongs here is that starting is never allowed to abandon anything by itself, and
        that the controls say which of the two is happening.
        """
        script = page_assets()["app.js"]
        starting = script[script.index("export async function start(") :]
        starting = starting[: starting.index("\n}")]
        assert "abandon();" not in starting, (
            "starting a search does not quietly end the one already running: refusing is what "
            "says so, and Stop is what does it"
        )
        assert "workingIndicator.hidden = !searching" in script
        assert "stopButton.hidden = !searching" in script, (
            "the stop control appears only while there is something to stop"
        )
        assert "startButton.disabled = searching" in script, (
            "and the search control says what pressing it now means, in being unpressable, rather "
            "than offering to restart a search the reader is watching"
        )
        assert 'startButton.textContent = searching' not in script, (
            "the control keeps its own name: a button relabelled to a different action is the "
            "same button that has to be refused"
        )
        assert '<button id="start-button" type="submit">Search</button>' in page_assets()["index.html"], (
            "and the name it keeps is the page's, written once in the markup"
        )
        assert "setWorking(true)" in script
        done = script[script.index("function abandon(") :]
        assert "setWorking(false)" in done[: done.index("\n}")], (
            "hanging up puts the controls back, because the search is over the moment the page "
            "stops reading it"
        )
        finally_block = starting[starting.index("} finally {") :]
        assert "setWorking(false)" in finally_block, (
            "the controls come back whatever happened: a search that failed and left the page "
            "still working leaves a page that can never be searched again"
        )

    def test_a_row_of_the_page_has_a_cell_for_every_column_the_page_declares(self) -> None:
        """The header row and a drawn row are decisions made in two files; they have to agree.

        The row is drawn by the adapter against a stand-in page, from the row a search really
        sends, so the columns are counted where they are made rather than where they are claimed.
        """
        headers = re.findall(r"<th\b", page_assets()["index.html"])
        [row] = in_the_adapter("rowFor", [[TestThePresentationItself.ROW]])
        cells = row["children"]
        assert len(cells) == len(headers) == 9, (
            f"the page declares {len(headers)} columns and a row draws {len(cells)}"
        )
        assert [cell["class"] for cell in cells] == [
            "name",
            "",
            "",
            "name",
            "",
            "",
            "",
            "",
            "",  # copy button cell (button inside has class 'copy-btn')
        ], "only the two names are marked-up runs; the rest is plain text"
        # The copy button is inside the last cell.
        copyCell = cells[8]
        assert copyCell["children"] == [{"tag": "button", "class": "copy-btn", "title": "Copy pair details to clipboard", "text": "⎘", "children": []}], (
            f"the last cell has the copy button: {copyCell}"
        )
        assert cells[6]["text"] == "9,876.543", cells[6]
        assert cells[7]["text"] == "1 differing character", cells[7]
        assert [span["text"] for span in cells[3]["children"]] == ["B", "nalac"]


class TestThePresentationItself:
    """The pure half of the front end, run outside a browser.

    These are the acceptance criteria that are about what a person reads: the price before the
    search, the parameters of an empty result, both halves of a row, and the differing
    characters standing out. They are checked by calling the same functions the page calls.
    """

    STARTED = {
        "type": "started",
        "search_id": "abc",
        "n": 1,
        "dataset": 5000,
        "dataset_label": "cities of 5,000 or more people",
        "database": "/srv/places.sqlite3",
        "min_population": None,
        "direction": "greatest",
        "top_k": 50,
        "places": 55108,
        "population_floor": 5000,
        "candidate_pairs": 139850045,
        "estimate": {
            "candidate_pairs": 139850045,
            "comparisons_per_second": None,
            "seconds": None,
            "basis": "not measured on this machine yet",
        },
    }

    ROW = {
        "first": {
            "geonameid": 1,
            "asciiname": "Analac",
            "display_name": "Analac",
            "latitude": 0.0,
            "longitude": 0.0,
            "population": 90000,
            "country_code": "XX",
        },
        "second": {
            "geonameid": 2,
            "asciiname": "Bnalac",
            "display_name": "Bnalac",
            "latitude": 39.0,
            "longitude": -89.0,
            "population": 80,
            "country_code": "YY",
        },
        "distance_km": 9876.54321,
        "character_difference": 1,
        "differing_positions": [0],
    }

    def test_numbers_are_read_as_counts_and_distances_as_distances(self) -> None:
        [thousands, small] = in_presentation("formatNumber", [[139850045], [3]])
        assert thousands == "139,850,045"
        assert small == "3"
        [distance] = in_presentation("formatDistance", [[9876.54321]])
        assert distance == "9,876.543", "a distance wants the same separators as a count"

    def test_the_price_of_a_search_is_said_before_it_runs(self) -> None:
        [words] = in_presentation("costWords", [[self.STARTED]])
        assert "139,850,045" in words, f"the count a person decides with: {words}"
        assert "cities of 5,000 or more people" in words
        assert "not measured" in words, (
            f"an unmeasured machine must not promise seconds: {words}"
        )
        assert words.startswith("cities of 5,000 or more people"), (
            f"the price is one sentence about the search, not a sentence with a hole in it: {words}"
        )

    def test_a_search_over_everything_says_there_is_no_floor_rather_than_a_floor_of_zero(
        self,
    ) -> None:
        """The whole-database choice really does answer `population_floor: 0`.

        A zero in that place is the absence of a rule rather than a rule about nothing, and a
        reader who sees "floor of 0" looks for the field they forgot to fill in.
        """
        whole_database = dict(self.STARTED)
        whole_database.update(
            {"dataset": None, "dataset_label": "every place in the database", "population_floor": 0}
        )
        [words] = in_presentation("costWords", [[whole_database]])
        assert words.startswith("every place in the database"), words
        assert "no population floor" in words, words
        assert "floor of 0" not in words, words

    def test_a_dataset_the_server_had_to_translate_is_quoted_back(self) -> None:
        """`requested_dataset` keeps the caller's spelling so a refusal can quote it; the price
        quotes it too, but only when the server actually had to work out what was meant."""
        [as_number, as_file] = in_presentation(
            "costWords",
            [[self.STARTED], [{**self.STARTED, "requested_dataset": "cities5000.txt"}]],
        )
        assert "asked for as" not in as_number, (
            f"5000 and 5000 are the one choice, and saying it twice is noise: {as_number}"
        )
        assert "asked for as cities5000.txt" in as_file, as_file
        assert as_file.startswith("cities of 5,000 or more people"), as_file

    def test_the_price_quotes_seconds_only_when_they_have_been_measured(self) -> None:
        measured = dict(self.STARTED)
        measured["estimate"] = {
            "candidate_pairs": 139850045,
            "comparisons_per_second": 4000000,
            "seconds": 35.0,
            # The server's own wording, quoted from a real /v1/estimate once one search on this
            # server had measured a pace: the page passes it through rather than paraphrasing it.
            "basis": "measured from an earlier search on this server",
        }
        [words] = in_presentation("costWords", [[measured]])
        assert "35" in words and "measured" in words, words
        assert words.startswith("cities of 5,000 or more people"), words

    def test_progress_says_pairs_compared_and_how_much_is_left(self) -> None:
        tick = {
            "type": "progress",
            "search_id": "abc",
            "places": 55108,
            "pairs_compared": 1203,
            "candidate_pairs": 139850045,
            "share_compared": 1203 / 139850045,
            "pairs": 4,
            "improvements": 2,
            "elapsed_seconds": 3.2,
            "estimated_seconds_remaining": 260.0,
        }
        [words] = in_presentation("progressWords", [[tick]])
        assert "1,203" in words and "139,850,045" in words, words
        assert "left" in words, f"a person wants to know how much more: {words}"
        assert "55,108" in words, f"the places being compared over: {words}"
        assert "about 4 min 20 s left" in words, f"the estimate is a guess, so it says so: {words}"

    def test_the_remaining_time_hedges_once_rather_than_twice(self) -> None:
        """A search that is nearly done reads "less than a second left", not "about less than".

        Real output from a real search of 27,666 places: the last tick said "about less than a
        second left", which is two hedges stacked and reads like a bug in the arithmetic.
        """
        nearly_done = {
            "type": "progress",
            "search_id": "abc",
            "places": 27666,
            "pairs_compared": 36885184,
            "candidate_pairs": 37306573,
            "share_compared": 0.989,
            "pairs": 6128,
            "improvements": 46,
            "elapsed_seconds": 18.1,
            "estimated_seconds_remaining": 0.4,
        }
        [words] = in_presentation("progressWords", [[nearly_done]])
        assert "less than a second left" in words, words
        assert "about less than" not in words, words

    def test_progress_admits_it_cannot_estimate_the_first_second(self) -> None:
        first_tick = {
            "type": "progress",
            "search_id": "abc",
            "places": 3,
            "pairs_compared": 0,
            "candidate_pairs": 6,
            "share_compared": 0.0,
            "pairs": 0,
            "improvements": 0,
            "elapsed_seconds": 0.01,
            "estimated_seconds_remaining": None,
        }
        [words] = in_presentation("progressWords", [[first_tick]])
        assert "no" in words.lower(), f"no pace yet has to read as no pace yet: {words}"

    def test_the_list_says_how_many_pairs_it_is_a_window_onto(self) -> None:
        """`seen_pairs` and `improvements` are what say whether waiting is worth it.

        A list of 50 rows says nothing about the search; 6,128 pairs found with the list improving
        46 times says the scan is still turning up better pairs, and 6,169 found with no
        improvement in the last minute says it has settled.
        """
        midway = {
            "type": "results",
            "search_id": "abc",
            "seen_pairs": 6128,
            "improvements": 46,
            "results": [],
        }
        [words] = in_presentation("listWords", [[midway]])
        assert "6,128" in words and "46" in words, words
        assert words.lower().startswith("searching"), words
        [one] = in_presentation("listWords", [[{"seen_pairs": 1, "improvements": 1}]])
        assert "1 pair found" in one and "1 time" in one, f"one pair, one time, not ones: {one}"

    def test_a_row_shows_both_places_their_countries_and_their_populations(self) -> None:
        [[first, second]] = in_presentation("rowCells", [[self.ROW]])
        assert first["name"] == "Analac" and second["name"] == "Bnalac"
        assert first["country"] == "XX" and second["country"] == "YY"
        assert first["population"] == "90,000" and second["population"] == "80", (
            "populations are shown as counts a person can compare, not as raw digits"
        )

    def test_a_row_carries_the_distance_and_the_number_of_differing_characters(self) -> None:
        [cells] = in_presentation("rowCells", [[self.ROW]])
        [first, second] = cells
        assert first["distance"] == "9,876.543"
        assert first["difference"] == "1 differing character", (
            f"the count the search was asked for, on the row: {first['difference']}"
        )

    def test_the_differing_characters_stand_out_and_the_rest_of_the_name_does_not(self) -> None:
        [[first, second]] = in_presentation("rowCells", [[self.ROW]])
        assert [(part["text"], part["differing"]) for part in first["parts"]] == [
            ("A", True),
            ("nalac", False),
        ], "the first character differs on this side"
        assert [(part["text"], part["differing"]) for part in second["parts"]] == [
            ("B", True),
            ("nalac", False),
        ]

    def test_every_differing_position_is_marked_on_both_names(self) -> None:
        """Two differing positions rather than one, so the runs between them have to survive.

        The pair and the positions come from the core: a fixture that named positions the pair
        does not actually differ at would still exercise the grouping, and would be a lie about a
        pair in the one place a reader goes to see what a row looks like.
        """
        first_name, second_name = "Analac", "Bnabac"
        positions = list(differing_position_indexes(first_name, second_name))
        assert positions == [0, 3], "the fixture is a pair that differs at two of them"

        row = {
            **self.ROW,
            "first": {**self.ROW["first"], "asciiname": first_name},
            "second": {**self.ROW["second"], "asciiname": second_name},
            "character_difference": len(positions),
            "differing_positions": positions,
        }
        [[first, second]] = in_presentation("rowCells", [[row]])
        assert [(part["text"], part["differing"]) for part in first["parts"]] == [
            ("A", True),
            ("na", False),
            ("l", True),
            ("ac", False),
        ], "the runs between the differing positions are kept, not split per character"
        assert [(part["text"], part["differing"]) for part in second["parts"]] == [
            ("B", True),
            ("na", False),
            ("b", True),
            ("ac", False),
        ], "the same positions, marked on the name that arrived with them"

    def test_the_differing_characters_reach_the_page_as_marked_spans(self) -> None:
        """The highlight is only real once it is an element, so the element is what is checked.

        The cell is built from the same row a search sends, cut into runs by the presentation
        module: what comes back is the markup a person looks at - the differing run marked, the
        run the two names share not marked - rather than a field that might be drawn someday.
        """
        [first, _second] = in_presentation("rowCells", [[self.ROW]])[0]
        [cell] = in_the_adapter("nameCell", [[first]])
        assert cell["tag"] == "td" and cell["class"] == "name", cell
        assert [(span["tag"], span["class"], span["text"]) for span in cell["children"]] == [
            ("span", "differing", "A"),
            ("span", "shared", "nalac"),
        ], "one span per run, marked the way the API said"
        assert "Analac" in cell["title"] and "1" in cell["title"], (
            f"the display name and the GeoNames id belong on the row: {cell['title']}"
        )

    def test_a_search_that_found_nothing_says_so_with_the_parameters_it_used(self) -> None:
        echo = {
            "n": 2,
            "dataset": 15000,
            "dataset_label": "cities of 15,000 or more people",
            "min_population": 100000,
            "direction": "least",
            "top_k": 50,
            "places": 5072,
        }
        done = {
            "type": "done",
            "search_id": "abc",
            "completed": True,
            "cancelled": False,
            "pairs": 0,
            "pairs_compared": 1420897,
            "elapsed_seconds": 1.2,
        }
        [words] = in_presentation("emptyWords", [[echo, done]])
        assert words.lower().startswith("no pair"), words
        assert "2" in words and "differing character" in words
        assert "cities of 15,000 or more people" in words
        assert "100,000" in words, f"the floor was one of the parameters: {words}"
        assert "least" in words, f"the direction was one of the parameters: {words}"

    def test_a_finished_search_reports_what_it_found_and_how_long_it_took(self) -> None:
        finished = {
            "type": "done",
            "search_id": "abc",
            "completed": True,
            "cancelled": False,
            "pairs": 6169,
            "pairs_compared": 37306573,
            "elapsed_seconds": 9.5,
        }
        [words] = in_presentation("finishedWords", [[finished]])
        assert "6,169" in words and "37,306,573" in words and "9.5" in words, words

    def test_a_cancelled_search_says_so_rather_than_claiming_to_have_finished(self) -> None:
        cancelled = {
            "type": "done",
            "search_id": "abc",
            "completed": False,
            "cancelled": True,
            "pairs": 3,
            "pairs_compared": 1000,
            "elapsed_seconds": 1.0,
            "cancel_reason": "stopped by SIGINT",
        }
        [words] = in_presentation("finishedWords", [[cancelled]])
        assert "cancel" in words.lower() and "SIGINT" in words, words
        assert "3" in words, "the list on screen is what the search found before it stopped"

    def test_a_refusal_arrives_as_the_servers_own_sentence(self) -> None:
        [words] = in_presentation(
            "refusalWords",
            [[409, {"error": "SearchAlreadyRunning", "message": "one search runs at a time"}]],
        )
        assert "one search runs at a time" in words, (
            "the server's sentence is the explanation; the page adds no diagnosis of its own"
        )
        assert "409" in words, "and the code, because a person pastes that to somebody else"

    def test_a_stream_is_read_line_by_line_without_waiting_for_the_end(self) -> None:
        """The list can only assemble during the scan if the client reads as it arrives.

        The unfinished tail goes in and comes back out, because that is the shape the adapter reads
        in: exactly one answer per read. It used to return one answer per *argument*, the page took
        the first of them, and the first was always the leftover tail - so every line the page read
        was parsed and thrown away, and the list stayed empty during a search that was running fine.
        """
        unfinished = '{"type":"progress","sea'
        first, second = in_presentation(
            "parseStream",
            [
                ["", '{"type":"started","search_id":"a"}\n' + unfinished],
                [unfinished, 'rch_id":"a"}\n{"type":"results","results":[]}\n'],
            ],
        )
        assert [event["type"] for event in first["events"]] == ["started"]
        assert first["rest"] == unfinished
        assert [event["type"] for event in second["events"]] == ["progress", "results"]
        assert second["rest"] == ""

    def test_the_dataset_choices_are_the_servers_words_in_order(self) -> None:
        listing = {
            "database": {"path": "/srv/p.sqlite3", "present": True, "places": 3, "imported": True},
            "datasets": [
                {"threshold": None, "label": "every place in the database", "places": 3, "candidate_pairs": 6},
                {"threshold": 5000, "label": "cities of 5,000 or more people", "places": 2, "candidate_pairs": 3},
            ],
        }
        [choices] = in_presentation("datasetChoices", [[listing]])
        assert [choice["value"] for choice in choices] == ["", "5000"], (
            "the whole-database choice sends no dataset at all"
        )
        assert [choice["label"] for choice in choices] == [
            "every place in the database",
            "cities of 5,000 or more people",
        ], "the server's wording is used verbatim: the page does not re-word a rule it did not set"
        assert "6" in choices[0]["detail"] and "3" in choices[1]["detail"]

    def test_an_unimported_cache_is_told_as_an_import_to_run(self) -> None:
        nothing_imported = {
            "database": {"path": "/srv/p.sqlite3", "present": False, "places": 0, "imported": False},
            "datasets": [],
        }
        [words] = in_presentation("databaseWords", [[nothing_imported]])
        assert "distnsimilarity-import" in words, words
        assert "/srv/p.sqlite3" in words, f"and which cache is empty: {words}"

    def test_a_populated_cache_says_what_will_be_searched(self) -> None:
        imported = {
            "database": {"path": "/srv/p.sqlite3", "present": True, "places": 146782, "imported": True},
            "datasets": [
                {"threshold": None, "label": "every place in the database", "places": 146782, "candidate_pairs": 927424318}
            ],
        }
        [words] = in_presentation("databaseWords", [[imported]])
        assert "146,782" in words and "/srv/p.sqlite3" in words, words

    def test_the_working_names_the_search_it_is_working_on(self) -> None:
        """`started` becomes the status line: which dataset, which threshold, which search."""
        [words] = in_presentation("searchingWords", [[self.STARTED]])
        assert words.startswith("Searching cities of 5,000 or more people"), words
        assert "differing by 1" in words, words
        assert "greatest distance first" in words, words
        assert "abc" in words, f"the id is how this search is asked about again: {words}"
        without_id = {key: value for key, value in self.STARTED.items() if key != "search_id"}
        [plain] = in_presentation("searchingWords", [[without_id]])
        assert "(search" not in plain, f"no id, so no parenthetical about one: {plain}"

class TestTheFrontEndAddsNoDomainLogic:
    """AC: "all matching and ranking come from the API"."""

    def test_the_page_never_measures_anything(self) -> None:
        """Every page file except the map, which is exempt from the trig and says why.

        `map.js` interpolates a great circle, and that needs `Math.cos` and friends. What it must
        not do is turn coordinates into a length, so `tests/test_browser_map.py` holds it to a
        narrower list of its own - no Earth radius, no kilometres, no reading of `distance_km`.
        """
        for name in MEASURED_ASSETS:
            text = page_assets()[name]
            found = [token for token in BANNED if token in text]
            assert found == [], (
                f"{name} contains {found}: a distance, a name comparison or a place count "
                "computed in the page is a second answer to a question the API already answered"
            )

    def test_the_page_reads_the_differing_positions_rather_than_finding_them(self) -> None:
        text = page_assets()["results.js"]
        assert "differing_positions" in text, "the highlight comes from the API's field"
        assert "charCodeAt" not in text and "codePointAt" not in text, (
            "comparing characters in the page is how two answers to `which characters differ` "
            "start disagreeing"
        )

    def test_the_page_never_sorts_the_list_it_was_given(self) -> None:
        """The order of the top-K is the ranking's, including which end is best."""
        for name in ("app.js", "results.js"):
            text = page_assets()[name]
            assert ".sort(" not in text and ".toSorted(" not in text and ".reverse()" not in text, (
                f"{name} re-orders a list the API already ranked"
            )

    def test_the_page_leaves_the_order_of_the_list_alone(self) -> None:
        """The order is the cores, including which end of it is the interesting end.

        `direction` is the one place that choice is made, and it is made in the core: a page that
        reversed the list for a nearest-neighbour search would be a second ranking, and would
        disagree with the CSV about which pairs are the best ones.
        """
        source = page_assets()["app.js"]
        assert (
            ".sort(" not in source
            and ".toSorted(" not in source
            and ".reverse()" not in source
            and "localeCompare" not in source
        ), "the page re-orders nothing, by a locale or any other way"
        assert "direction" not in source, (
            "there is no branch on which end is best: the form forwards the choice, and the page "
            "draws the order it was given"
        )
        assert "new FormData(form)" in parameters_of(source), (
            "the parameters leave as the form holds them, which is the only way the direction the "
            "price was quoted for is the direction that runs"
        )

    def test_the_page_sends_no_parameter_the_api_does_not_take(self) -> None:
        """`source` is refused with 410; a page still sending it would be its own worst client."""
        text = page_assets()["app.js"]
        assert "source" not in re.sub(r'"[a-z_]*"', "", text).replace("resource", ""), (
            "the page must not ask which source to read: the place database is the only one"
        )

    @pytest.mark.parametrize("name", ["app.js", "results.js", "map.js"])
    def test_the_script_is_valid_module_source(self, name: str, tmp_path: Path) -> None:
        if shutil.which("node") is None:
            pytest.skip("the syntax of the front end is checked with Node, which is not installed")
        path = {"app.js": APP_JS, "results.js": RESULTS_JS, "map.js": MAP_JS}[name]
        completed = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, f"{name} does not parse: {completed.stderr.strip()}"
