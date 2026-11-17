"""The browser front end: what it asks the API for, and what it must never do for itself.

There is no browser in this suite, so the contract is held from both ends. From the server:
the page and its two assets are served, and every field the page renders exists on a real
streamed search (`THE_WIRE` below is checked against events, not against a comment). From the
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
from typing import Any

import pytest

from distnsimilarity.core.matching import differing_position_indexes
from http_support import events_of, serve
from support import dump_row, place_database

STATIC = Path(__file__).resolve().parent.parent / "src" / "distnsimilarity" / "api" / "static"
HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
RESULTS_JS = STATIC / "results.js"
STYLES = STATIC / "styles.css"

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
    "the estimate": ["estimate", "seconds", "basis", "comparisons_per_second", "population_floor"],
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
    ],
    "a progress event": [
        "pairs_compared",
        "share_compared",
        "improvements",
        "elapsed_seconds",
        "estimated_seconds_remaining",
    ],
    "a results event": ["seen_pairs", "results"],
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
    "a done event": ["completed", "cancelled", "pairs", "error", "cancel_reason", "retryable"],
    "an error event": ["message"],
}

#: Everything a page is allowed to contain without the API's say-so. Trig, powers and a square
#: root together are a distance formula; `differing_positions:` and `character_difference:`
#: written by the page are a second opinion about what a pair looks like; `http` in a `src` or
#: `href` is somebody else's server in the middle of a tool that runs on one laptop.
BANNED = (
    "Math.sqrt",
    "Math.pow",
    "Math.hypot",
    "Math.sin",
    "Math.cos",
    "Math.tan",
    "Math.asin",
    "Math.acos",
    "Math.atan",
    "6371",
    "haversine",
    "great_circle",
    "levenshtein",
    "differing_positions:",
    "character_difference:",
    "distance_km:",
)


def page_assets() -> dict[str, str]:
    """The four files the front end is made of, by name."""
    assert STATIC.is_dir(), f"the front end should live in {STATIC}"
    assets = {"index.html": HTML, "app.js": APP_JS, "results.js": RESULTS_JS, "styles.css": STYLES}
    missing = [name for name, path in assets.items() if not path.is_file()]
    assert not missing, f"the front end is missing {missing}"
    return {name: path.read_text(encoding="utf-8") for name, path in assets.items()}


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


#: Read one exported function of `results.js` by name, apply it to each case in turn, and print
#: the results as JSON. `process.argv` carries the module, the function and the cases, because a
#: JSON payload interpolated into a JavaScript literal is a quoting bug waiting to be found.
PRESENTATION_SCRIPT = """
import { pathToFileURL } from "node:url";
const presentation = await import(pathToFileURL(process.argv[1]).href);
const underTest = presentation[process.argv[2]];
const cases = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify(cases.map((sent) => underTest(...sent))));
"""


def run_in_node(script: str, *arguments: str) -> Any:
    """Run a snippet with Node and read the JSON it prints.

    Node is a developer's tool here, not a dependency: where it is not installed the tests that
    need it skip, and `uv run pytest` still means one command on a machine with only the
    packages in `pyproject.toml`.
    """
    if shutil.which("node") is None:
        pytest.skip("the presentation module is checked with Node, which is not installed here")
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the front end's presentation module did not run: {completed.stderr.strip()}"
    )
    return json.loads(completed.stdout.strip())


def in_presentation(function: str, cases: list[list[object]]) -> list[object]:
    """Apply one exported function of `results.js` to each case, where a case is one call's arguments.

    One case per call rather than one big call, because most of these functions are asked about
    one event, and `formatNumber` is asked twice in one test; `[[one, two]]` is how a function of
    two arguments - `emptyWords`, `refusalWords` - is given them.
    """
    return run_in_node(
        PRESENTATION_SCRIPT, str(RESULTS_JS), function, json.dumps(cases)
    )


#: `nameCell` turns data into elements, and Node has no page to make them from, so the check that
#: the highlight reaches the page brings its own stand-in document. It offers the three things the
#: marking uses - make an element, append to it, set its text - on purpose: an implementation that
#: needed more of a browser would be a sign the marking is no longer one small function's job.
ADAPTER_SCRIPT = """
import { pathToFileURL } from "node:url";
const adapter = await import(pathToFileURL(process.argv[1]).href);
const underTest = adapter[process.argv[2]];
const cases = JSON.parse(process.argv[3]);

function element(tag) {
  return {
    tagName: tag,
    children: [],
    className: "",
    title: "",
    textContent: "",
    dataset: {},
    append(...nodes) {
      this.children.push(...nodes);
    },
  };
}

function describe(node) {
  return {
    tag: node.tagName,
    class: node.className,
    title: node.title,
    text: node.textContent,
    children: node.children.map(describe),
  };
}

const page = { createElement: element };
const made = cases.map((sent) => underTest(...sent, page));
process.stdout.write(JSON.stringify(made.map(describe)));
"""


def in_the_adapter(function: str, cases: list[list[object]]) -> list[object]:
    """Apply one exported function of `app.js` to each case, with the stand-in document last."""
    return run_in_node(ADAPTER_SCRIPT, str(APP_JS), function, json.dumps(cases))


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
        assert sent == ["dataset", "direction", "min_population", "n", "top_k"], (
            f"these names go out as query parameters: {sent}"
        )
        script = page_assets()["app.js"]
        assert "new FormData(form)" in script, "the form is forwarded as it stands"
        assert "${ESTIMATE_ENDPOINT}?" in script and "${SEARCH_ENDPOINT}?" in script, (
            "one set of parameters pays for the estimate and runs the search, so the price cannot "
            "be quoted for a different search from the one that starts"
        )
        for invented in ("source", "csv", "sort", "offset"):
            assert f'"{invented}"' not in script and f'name="{invented}"' not in page, (
                f"the page sends {invented}, which this API does not take"
            )

    def test_a_control_left_empty_sends_nothing_rather_than_a_blank_value(self) -> None:
        """A population floor nobody typed is the absence of a rule, not a rule about nothing."""
        script = page_assets()["app.js"]
        sent = script[script.index("function parametersToSend(") :]
        assert 'if (text !== "")' in sent[: sent.index("\n}")], (
            "sending an empty floor as a blank value is a refusal about a field nobody filled in"
        )
        assert "new URLSearchParams()" in sent[: sent.index("\n}")], (
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
        body = script[script.index("async function start(") :]
        assert body.index(
            "estimateLine.textContent = costWords(await fetchJSON(`${ESTIMATE_ENDPOINT}"
        ) < body.index("await watch("), (
            "inside starting a search, the cost is asked for and put on the page before the "
            "stream is opened: afterwards it is a receipt rather than a decision"
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

    def test_pressing_search_again_restarts_rather_than_running_two_searches(self) -> None:
        """Two streams drawing one list is a page that disagrees with itself about what is running."""
        script = page_assets()["app.js"]
        starting = script[script.index("async function start(") :]
        assert starting.index("abandon();") < starting.index("new AbortController()"), (
            "the search still being read is hung up on before the next one is asked for"
        )
        assert "workingIndicator.hidden = !searching" in script
        assert "stopButton.hidden = !searching" in script, (
            "the stop control appears only while there is something to stop"
        )
        assert 'startButton.textContent = searching ? "Restart" : "Search"' in script, (
            "and the same control says what pressing it now means"
        )
        assert "setWorking(true)" in script
        done = script[script.index("function abandon(") :]
        assert "setWorking(false)" in done
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
        assert len(cells) == len(headers) == 8, (
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
        ], "only the two names are marked-up runs; the rest is plain text"
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
        """The list can only assemble during the scan if the client reads as it arrives."""
        [[first, second]] = in_presentation(
            "parseStream",
            [
                [
                    '{"type":"started","search_id":"a"}\n{"type":"progress","sea',
                    'rch_id":"a"}\n{"type":"results","results":[]}\n',
                ]
            ],
        )
        assert [event["type"] for event in first["events"]] == ["started"]
        assert first["rest"] == '{"type":"progress","sea'
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
        for name, text in page_assets().items():
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
        sent = source[source.index("function parametersToSend(") :]
        assert "new FormData(form)" in sent[: sent.index("\n}")], (
            "the parameters leave as the form holds them, which is the only way the direction the "
            "price was quoted for is the direction that runs"
        )

    def test_the_page_sends_no_parameter_the_api_does_not_take(self) -> None:
        """`source` is refused with 410; a page still sending it would be its own worst client."""
        text = page_assets()["app.js"]
        assert "source" not in re.sub(r'"[a-z_]*"', "", text).replace("resource", ""), (
            "the page must not ask which source to read: the place database is the only one"
        )

    @pytest.mark.parametrize("name", ["app.js", "results.js"])
    def test_the_script_is_valid_module_source(self, name: str, tmp_path: Path) -> None:
        if shutil.which("node") is None:
            pytest.skip("the syntax of the front end is checked with Node, which is not installed")
        path = {"app.js": APP_JS, "results.js": RESULTS_JS}[name]
        completed = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, f"{name} does not parse: {completed.stderr.strip()}"
