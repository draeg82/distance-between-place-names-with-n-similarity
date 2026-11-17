"""Streaming a search over HTTP: partial results, cost up front, and one search at a time.

Issue #6's contract, tested against a real server on the loopback interface with a real
client: newline-delimited JSON, the top-K improving while the scan is still running, the cost
of a search answerable before committing to it, improvements coalesced rather than streamed
one per change, and a second search refused with a reason. A server is started per test on an
ephemeral port, and the suite stays offline (`tests/conftest.py` allows loopback and refuses
anything else).

Two fixtures carry the timing-sensitive tests. `noisy_database` is one planted pair and a long
stretch of equal-length names that match nothing - a search that takes a while and finds one
thing. `many_pair_database` is families of names where everything matches several others, and
later families sit further away, so the best-so-far keeps improving over the whole scan.

The properties asserted over and over are the ones the ticket is about: that a `results` event
reached the socket *while the scan was still running*; that the list the browser would draw is
the core's own ranking of the same pairs; and that a search nobody is reading *stops doing
work*, measured with the registry and the cancellation polling counters rather than hoped.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn

from distnsimilarity.api.app import create_app
from distnsimilarity.api.registry import SearchRecord, SearchRegistry
from distnsimilarity.core.distance import great_circle_distance_km
from distnsimilarity.core.matching import differing_position_indexes
from distnsimilarity.core.scan import MatchedPair, matches_character_difference
from distnsimilarity.core.topk import DEFAULT_TOP_K, Ranking, TopK, snapshots
from distnsimilarity.search import SearchRequest, open_search
from distnsimilarity.api import watch as watch_cli
from distnsimilarity.api.watch import parse_events
from support import (
    NOISY_DUMP_PAIR,
    dump_row,
    noisy_database,
    place_database,
)

#: Generous, because these tests wait for a real server to come up and for a real client to
#: hang up; short enough that a hang shows up as a failure rather than a stalled suite.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
WAIT_LIMIT = 10.0

#: A database big enough that searching it takes over a second: long enough for a client to read a
#: find from the stream and still catch the search running, and short enough that a suite
#: waiting on it is not slow. `noisy_dump` plants its pair at the head of the file.
SLOW_PLACES = 2_500


#: A search that produces pairs one step at a time, so that the pacing of the stream - what
#: gets coalesced, how often progress ticks - is something a test can see rather than miss.
LIVELY = {"batch_size": 1, "step_seconds": 0.001}


@dataclass
class RunningServer:
    """A search server on an ephemeral port, plus the registry the requests are writing to."""

    base_url: str
    server: uvicorn.Server
    thread: threading.Thread
    registry: SearchRegistry

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )

    def get(self, path: str, **params: object) -> httpx.Response:
        with self.client() as client:
            return client.get(path, params=params or None)

    def search(self, n: int = 1, **params: object) -> httpx.Response:
        return self.get("/v1/search", n=n, **params)

    def estimate(self, n: int = 1, **params: object) -> httpx.Response:
        return self.get("/v1/estimate", n=n, **params)

    def record(self, search_id: str) -> SearchRecord:
        record = self.registry.get(search_id)
        assert record is not None, f"the server has forgotten search {search_id}"
        return record

    def wait_for(self, description: str, predicate) -> None:
        """Wait for something the server does on another thread, and say what never happened."""
        deadline = time.monotonic() + WAIT_LIMIT
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"timed out after {WAIT_LIMIT}s waiting for {description}")


@contextmanager
def serve(database: Path, **app_keywords: object) -> Iterator[RunningServer]:
    """Run `create_app` over one place cache, on 127.0.0.1, with a port the OS picks.

    The cache is given the way `--database` gives it. Passing `None` gives the server no cache at
    all, which is how a test proves it looks in the directory `--data-dir` names, or
    `$DISTNSIMILARITY_DB`, or beside the dumps - rather than reaching for a dump. The search is
    stepped on worker threads and the events are written from the event loop, so a test that wants
    to know whether the scan is *still running* asks the same registry the requests are writing
    to, rather than guessing from wall-clock time.
    """
    registry = SearchRegistry()
    app = create_app(
        database=database, registry=registry, **app_keywords  # type: ignore[arg-type]
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-under-test", daemon=True)
    thread.start()

    deadline = time.monotonic() + CONNECT_TIMEOUT
    while not server.started:
        if not thread.is_alive():
            raise AssertionError("the search server thread died while starting")
        if time.monotonic() > deadline:
            raise AssertionError(f"the search server did not start within {CONNECT_TIMEOUT}s")
        time.sleep(0.005)

    host, port = server.servers[0].sockets[0].getsockname()[:2]
    try:
        yield RunningServer(
            base_url=f"http://{host}:{port}", server=server, thread=thread, registry=registry
        )
    finally:
        server.should_exit = True
        thread.join(timeout=CONNECT_TIMEOUT)


def many_pair_database(
    path: Path, *, families: int = 6, per_family: int = 4, widening: bool = True
) -> Path:
    """A cache with a lot of pairs at `n=1`, whose best find keeps changing as the scan goes on.

    `towns01` and `towne01` differ in one position, and so do `towns01` and `towns02`, so every
    name in a family matches several others. A family's names sit `widening` apart in
    proportion to which family it is, so the pairs found in a later family are further apart
    than those in an earlier one: the best find at rank 1 keeps improving across the whole scan
    instead of settling in the first hundred comparisons. Names in different families differ in
    two positions, so they are never a pair, and within a family the pairs between neighbours
    are nearly on top of each other.

    `widening=False` reverses that, which is what a least-first search needs to see the same
    kind of improvement at the other end of the range.

    Populations fall with the identifier, and a cache walk goes by increasing population, so a
    search walks these places in the reverse of the order they were written in: the last family
    written is the first one compared. `widening` says which end of the scan the families at
    the far end of the range sit at, and so which end the best-so-far list keeps improving from.
    """
    rows = []
    identifier = 0
    for family in range(families):
        apart = 0.05 * (family + 1 if widening else families - family)
        for index in range(per_family):
            identifier += 1
            rows.append(
                dump_row(
                    identifier,
                    f"towns{family}{index}",
                    10.0 + 0.01 * index,
                    -100.0,
                    population=100_000 - identifier,
                )
            )
            identifier += 1
            rows.append(
                dump_row(
                    identifier,
                    f"towne{family}{index}",
                    10.0 + 0.01 * index,
                    -100.0 + apart,
                    population=100_000 - identifier,
                )
            )
    return place_database(path, rows)


#: The pair `many_pair_database` produces in every family, at `n=1`.
MANY_PAIR_FAMILY_DISTANCE_STEP = 0.05


def silent_database(path: Path, *, places: int = 10) -> Path:
    """A cache with candidate pairs to compare and nothing to find.

    Every name is `noise` plus digits whose sum is a multiple of ten. Any two of them are the
    same length, so the equal-length prefilter keeps every pair, and changing one digit can
    never keep the sum a multiple of ten, so any two differ in at least two positions and none
    of them is a pair at n=1. A search over this cache is all scan and no results, which is
    what a progress event has to be able to prove on its own. `places` sizes it: a few hundred
    makes a scan long enough to see more than one progress tick.
    """
    names: list[str] = []
    for value in range(10_000):
        digits = f"{value:04d}"
        if sum(int(digit) for digit in digits) % 10 == 0:
            names.append(f"noise{digits}")
            if len(names) == places:
                break
    assert len(names) == places, f"the code only makes {len(names)} names, not {places}"
    assert all(
        not matches_character_difference(a, b, 1)
        for a in names
        for b in names
        if a is not b
    ), "the fixture is a code with distance 2, or the test below means nothing"
    return place_database(
        path,
        [
            dump_row(
                position + 1,
                name,
                -60.0 + (position % 80),
                -140.0 + (position % 160),
                population=10_000,
            )
            for position, name in enumerate(names)
        ],
    )


def pair_database(path: Path) -> Path:
    """The smallest cache worth searching: one pair at `n=1`, and one small place with it."""
    return place_database(
        path,
        [
            dump_row(1, "abelard", 44.0, -93.0, population=50_000),
            dump_row(2, "abelurd", 39.0, -89.0, population=40_000),
            dump_row(3, "small", 1.0, 1.0, population=7),
        ],
    )


def parse_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def events_of(response: httpx.Response) -> list[dict[str, Any]]:
    """Parse a whole streamed body as newline-delimited JSON."""
    assert response.headers["content-type"].startswith("application/x-ndjson"), (
        f"a streamed search is ndjson, got {response.headers['content-type']!r}"
    )
    return parse_lines(response.text)


def types_of(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["type"]) for event in events]


def of_type(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == kind]


def started_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    return of_type(events, "started")[0]


def done_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    return of_type(events, "done")[0]


def found_names(event: dict[str, Any]) -> list[tuple[str, str]]:
    """The top-K in one `results` event, in the order the server sent it."""
    return [(row["first"]["asciiname"], row["second"]["asciiname"]) for row in event["results"]]


def found_distances(event: dict[str, Any]) -> list[float]:
    return [float(row["distance_km"]) for row in event["results"]]


def headless_matches(database: Path, n: int, **request_keywords: object) -> list[MatchedPair]:
    """What the same request produces with no HTTP in the way - the seam, walked to the end."""
    search_request = SearchRequest(
        database=database, n=n, **request_keywords  # type: ignore[arg-type]
    )
    return list(open_search(search_request).pairs)


def headless_pairs(
    database: Path, n: int, **request_keywords: object
) -> list[tuple[str, str, float]]:
    return [
        (match.first.asciiname, match.second.asciiname, match.distance_km)
        for match in headless_matches(database, n, **request_keywords)
    ]


def headless_top_k(
    database: Path,
    n: int,
    *,
    top_k: int = DEFAULT_TOP_K,
    direction: Ranking = Ranking.GREATEST,
    **request_keywords: object,
) -> list[tuple[str, str]]:
    """The same top-K the server reports, ranked by the core over the seam's own stream."""
    ranked = TopK.of(
        headless_matches(database, n, **request_keywords), limit=top_k, direction=direction
    )
    return [(row.first.asciiname, row.second.asciiname) for row in ranked.snapshot()]


class TestTheServiceIsReachable:
    def test_it_answers_and_says_where_the_places_are(self, tmp_path: Path) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.get("/healthz")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
        assert response.json()["searches"]["live"] == 0
        assert response.json()["database"] == {
            "path": str(database),
            "present": True,
            "places": 3,
            "imported": True,
        }, "the first thing a caller needs to know is whether this server has any places"

    def test_it_lists_the_datasets_it_can_search_and_the_state_of_the_cache(
        self, tmp_path: Path
    ) -> None:
        """The dropdown is the cache's own answer, in words a person can choose from."""
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Aa", 1.0, 2.0, population=40_000),
                dump_row(2, "Bb", 3.0, 4.0, population=9_000),
                dump_row(3, "Cc", 5.0, 6.0, population=3_000),
            ],
            source_threshold=1_000,
        )

        with serve(database) as server:
            body = server.get("/v1/datasets").json()

        assert [option["label"] for option in body["datasets"]] == [
            "every place in the database",
            "cities of 5,000 or more people",
            "cities of 15,000 or more people",
        ], "a dataset is worded as a rule about people, never as a bare number or a file name"
        assert [option["places"] for option in body["datasets"]] == [3, 2, 1], (
            f"each choice says how many places it searches, got {body['datasets']}"
        )
        assert [option["candidate_pairs"] for option in body["datasets"]] == [6, 3, 1], (
            "and what it will cost, so a person can see before they click what the server said"
        )
        assert body["database"] == {
            "path": str(database),
            "present": True,
            "places": 3,
            "imported": True,
        }

    def test_a_cache_only_offers_the_choices_its_own_places_support(
        self, tmp_path: Path
    ) -> None:
        """Three dumps were published, but a cache imported from one of them is not obliged to
        offer three choices that would all search the same places.

        Both places here clear every threshold the tool names, so `cities of 1,000 or more
        people` would be the whole cache wearing a smaller number.
        """
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Aa", 1.0, 2.0, population=40_000),
                dump_row(2, "Bb", 3.0, 4.0, population=20_000),
            ],
            source_threshold=15_000,
        )

        with serve(database) as server:
            options = server.get("/v1/datasets").json()["datasets"]

        assert [option["threshold"] for option in options] == [None], (
            f"every threshold here would search the same two places: {options}"
        )
        assert options[0]["places"] == 2

    def test_the_database_is_reported_without_being_created(self, tmp_path: Path) -> None:
        missing = tmp_path / "places.sqlite3"

        with serve(missing) as server:
            body = server.get("/v1/datasets").json()
            health = server.get("/healthz").json()

        assert body["datasets"] == [], "a cache that is not there offers no datasets"
        assert body["database"] == {
            "path": str(missing),
            "present": False,
            "places": 0,
            "imported": False,
        }
        assert health["database"]["present"] is False
        assert not missing.exists(), (
            "asking what is searchable must not leave an empty place cache behind"
        )

    def test_it_lists_nothing_before_anything_has_been_searched(self, tmp_path: Path) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            body = server.get("/v1/searches").json()

        assert body == {"live": [], "recent": []}

    def test_a_server_given_nothing_serves_the_cache_the_tool_defaults_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`DISTNSIMILARITY_DB` is how a deployment names its places once, for every command.

        `distnsimilarity-serve` with neither `--database` nor `--data-dir` has to honour it, or
        one machine answers two questions about which places it serves, and the terminal and the
        browser search different databases while agreeing they searched the same one.
        """
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Aa", 1.0, 2.0, population=40_000),
                dump_row(2, "Bb", 3.0, 4.0, population=20_000),
                dump_row(3, "Ab", 5.0, 6.0, population=10_000),
            ],
        )
        monkeypatch.setenv("DISTNSIMILARITY_DB", str(database))

        with serve(None) as server:
            body = server.get("/healthz").json()

        assert body["database"]["path"] == str(database.resolve())
        assert body["database"]["places"] == 3, "the server found that cache and opened it"
        assert body["database"]["imported"] is True

    def test_the_search_running_now_is_the_one_the_list_lists(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                search_id = r.headers["X-Search-Id"]
                server.wait_for(
                    "the running search to be listed as live",
                    lambda: [
                        row["search_id"] for row in server.get("/v1/searches").json()["live"]
                    ]
                    == [search_id],
                )
                body = server.get("/v1/searches").json()

        row = body["live"][0]
        assert row["dataset"] is None, "the request named no dataset: the whole cache is searched"
        assert row["database"] == str(database)
        assert row["request"]["n"] == 1
        assert row["live"] is True
        assert row["candidate_pairs"] > 0, "a live search knows the size of its own job"
        assert row["retryable"] is False, "a search still running is not one to run again"
        assert body["recent"] == [], "nothing has ended yet"


class TestStreamingASearch:
    """The event protocol: what a client reads, and in what order."""

    def test_a_search_is_json_lines_opening_with_the_plan_and_closing_with_the_totals(
        self, tmp_path: Path
    ) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.search(1)

        assert response.status_code == 200, response.text
        events = events_of(response)
        kinds = types_of(events)
        assert kinds[0] == "started", "the first line says what is being searched and what it costs"
        assert kinds[-1] == "done", "the last line sums the search up"
        assert set(kinds[1:-1]) <= {"progress", "results"}, "only progress and finds go between"
        assert of_type(events, "results"), "a search that found pairs must say which"

    def test_the_opening_event_reports_the_request_back_and_adds_what_it_resolved_to(
        self, tmp_path: Path
    ) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "abelard", 44.0, -93.0, population=50_000),
                dump_row(2, "abelurd", 39.0, -89.0, population=40_000),
                dump_row(3, "small", 1.0, 1.0, population=7),
            ],
        )

        with serve(database) as server:
            events = events_of(server.search(1, min_population=1_000))
            listed = server.get("/v1/searches").json()

        started = started_of(events)
        assert started["requested_dataset"] is None, "this request named no dataset"
        assert started["dataset"] is None, "and so the whole cache is searched"
        assert started["dataset_label"] == "every place in the database"
        assert started["database"] == str(database), "and the event says which cache"
        assert "source" not in started, (
            "there is one search source, so a request and an event do not name it"
        )
        assert started["n"] == 1
        assert started["min_population"] == 1_000
        assert started["equal_length_prefilter"] is True

        # ... and what that request became, which is not always the same string.
        assert started["places"] == 2, "the candidate places, after the floor took effect"
        assert started["population_floor"] == 1_000
        assert started["candidate_pairs"] == 3, (
            "two candidate places, each against itself and the other: n=0 matches a place with"
            " itself, so the count of work includes those comparisons"
        )
        assert started["improvements"] == 0
        assert started["elapsed_seconds"] == 0.0

        search_id = started["search_id"]
        assert search_id, "the opening event names the search it has just opened"
        assert listed["live"] == []
        assert listed["recent"][0]["search_id"] == search_id

    def test_each_request_for_the_same_thing_opens_its_own_search(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "abelard", 44.0, -93.0, population=50_000),
                dump_row(2, "abelurd", 39.0, -89.0, population=40_000),
            ],
        )

        with serve(database) as server:
            first = started_of(events_of(server.search(1)))
            second = started_of(events_of(server.search(1)))
            listed = server.get("/v1/searches").json()["recent"]

        assert first["search_id"] != second["search_id"], (
            "the same request twice is two searches, each with its own id to list and cancel"
        )
        assert [record["search_id"] for record in listed] == [
            first["search_id"],
            second["search_id"],
        ], "the registry lists every search the server has run, oldest first"

    def test_a_dataset_can_be_named_by_its_population_threshold(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "abelard", 44.0, -93.0, population=55_000),
                dump_row(2, "abelurd", 39.0, -89.0, population=60_000),
                dump_row(3, "tinyton", 1.0, 1.0, population=500),
            ],
            source_threshold=1_000,
        )

        with serve(database) as server:
            events = events_of(server.search(1, dataset="50000"))

        started = started_of(events)
        assert started["dataset"] == 50_000, "a dataset is a population threshold, not a file"
        assert started["requested_dataset"] == "50000", "and the event keeps the spelling sent"
        assert started["dataset_label"] == "cities of 50,000 or more people"
        assert found_names(of_type(events, "results")[-1]) == [NOISY_DUMP_PAIR]

    def test_the_event_protocol_carries_the_search_id_on_every_line(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            events = events_of(server.search(1))

        search_id = started_of(events)["search_id"]
        assert {event["search_id"] for event in events} == {search_id}, (
            "one connection, one search: every event names it, so a client cannot mix two up"
        )

    def test_the_stream_arrives_in_several_pieces_rather_than_one_buffered_body(
        self, tmp_path: Path
    ) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=8, per_family=5)

        with serve(database, **LIVELY) as server, server.client() as client:
            pieces = []
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                for piece in r.iter_raw():
                    pieces.append(piece)

        assert len(pieces) > 1, f"the whole search arrived as one chunk: {len(pieces)} piece(s)"
        kinds = types_of(parse_lines(b"".join(pieces).decode("utf-8")))
        assert kinds[0] == "started" and kinds[-1] == "done", (
            f"the pieces did not make one whole search: {kinds}"
        )


class TestTheRowsTheClientDraws:
    """What one row of the top-K needs: both names, the countries, the populations, the
    distance, how many positions differ and which ones, and both geonameids."""

    def test_a_found_pair_carries_everything_a_row_and_a_map_need(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(
                    101,
                    "abelard",
                    44.0,
                    -93.0,
                    population=50_000,
                    countrycode="US",
                ),
                dump_row(
                    202,
                    "abelurd",
                    39.0,
                    -89.0,
                    population=40_000,
                    countrycode="CA",
                ),
            ],
        )

        with serve(database) as server:
            events = events_of(server.search(1))

        row = of_type(events, "results")[-1]["results"][0]
        # Which half of a pair is `first` is the database's doing, not the caller's: the cache is
        # walked in ascending population, so the smaller city comes first and the row order the
        # places were imported in says nothing about it.
        assert [row["first"]["geonameid"], row["second"]["geonameid"]] == [202, 101], (
            "the ids are what a client follows up on; a row without them is decoration"
        )
        assert [row["first"]["asciiname"], row["second"]["asciiname"]] == list(
            reversed(NOISY_DUMP_PAIR)
        )
        assert [row["first"]["country_code"], row["second"]["country_code"]] == ["CA", "US"]
        assert [row["first"]["population"], row["second"]["population"]] == [40_000, 50_000]
        assert row["distance_km"] == great_circle_distance_km(39.0, -89.0, 44.0, -93.0)
        assert row["character_difference"] == 1, (
            "how many positions differ, which is not the same as the requested n once n is 0"
        )
        assert row["differing_positions"] == list(differing_position_indexes(*NOISY_DUMP_PAIR))
        assert len(row["differing_positions"]) == row["character_difference"]

    def test_a_self_pair_search_reports_pairs_at_zero_too(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Lima", -12.0, -77.0, population=100),
                dump_row(2, "Lima", -12.0, -77.0, population=200),
                dump_row(3, "Lima", -33.0, 151.0, population=300),
            ],
        )
        expected = headless_top_k(database, 0, top_k=25)
        assert len(expected) == 6, "three Limas make three pairs and three self-pairs at n=0"

        with serve(database) as server:
            events = events_of(server.search(0))

        assert found_names(of_type(events, "results")[-1]) == expected


class TestTheBestSoFarImproves:
    """`results` events: partial results a browser can watch, ranked by the core."""

    def test_the_first_find_reaches_the_client_while_the_scan_is_still_running(
        self, tmp_path: Path
    ) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                events = []
                for event in parse_events(r.iter_lines()):
                    events.append(event)
                    if event["type"] == "results":
                        # The search is open in the server's own registry at this very moment,
                        # so this is a partial result and not a report of a finished scan.
                        assert server.get("/healthz").json()["searches"]["live"] == 1
                        break

        assert found_names(events[-1]) == [NOISY_DUMP_PAIR]

    def test_no_event_ever_shows_a_worse_list_than_the_one_before_it(
        self, tmp_path: Path
    ) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=7, per_family=5)

        with serve(database, results_interval=0.0) as server:
            events = events_of(server.search(1, top_k=5))

        lists = [found_distances(event) for event in of_type(events, "results")]
        assert len(lists) > 1, f"the stream reported the best so far once: {lists}"
        for earlier, later in zip(lists, lists[1:]):
            assert len(later) >= len(earlier), "the list never loses a find"
            assert all(
                later_distance >= earlier_distance
                for later_distance, earlier_distance in zip(later, earlier)
            ), f"a client would have seen the list get worse: {earlier} then {later}"
            assert later != earlier, "an event is only sent when the list has changed"
        assert lists[-1][0] > lists[0][0], "the best find at the end beats the best at the start"

    def test_the_least_first_direction_flips_which_end_is_best(self, tmp_path: Path) -> None:
        # The families get closer together rather than further apart, so a least-first search
        # has something new to report late in the scan rather than only at the start.
        database = many_pair_database(
            tmp_path / "places.sqlite3", families=7, per_family=5, widening=False
        )

        with serve(database, results_interval=0.0, **LIVELY) as server:
            events = events_of(server.search(1, direction="least", top_k=5))

        started = started_of(events)
        assert started["direction"] == "least"
        lists = [found_distances(event) for event in of_type(events, "results")]
        assert len(lists) > 1, f"the least-first search reported its best once: {lists}"
        for earlier, later in zip(lists, lists[1:]):
            assert all(
                later_distance <= earlier_distance
                for later_distance, earlier_distance in zip(later, earlier)
            ), f"least-first means the list only gets closer: {earlier} then {later}"
        assert lists[0][0] > lists[-1][0], f"the closest pair should win: {lists[-1][0]}"

        final = of_type(events, "results")[-1]
        assert found_distances(final) == [
            row.distance_km
            for row in TopK.of(
                headless_matches(database, 1),
                limit=final["top_k"],
                direction=Ranking.LEAST,
            ).snapshot()
        ], "the list a least-first browser would draw is the core's own, in its own order"

    def test_the_searched_list_is_bounded_by_top_k_and_the_core_ranks_it(
        self, tmp_path: Path
    ) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=8, per_family=5)

        with serve(database, **LIVELY) as server:
            events = events_of(server.search(1, top_k=3))

        results = of_type(events, "results")
        assert all(len(event["results"]) <= 3 for event in results), (
            "the search keeps `top_k` finds and no more, however many it has seen"
        )
        assert results[-1]["top_k"] == 3
        assert found_names(results[-1]) == headless_top_k(database, 1, top_k=3), (
            "the list the browser would draw is the core's ranking of the pairs the seam emits"
        )
        assert done_of(events)["pairs"] >= 3

    def test_the_sequence_of_lists_is_the_one_the_core_produces(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=6, per_family=4)
        top_k = 4

        with serve(database, results_interval=0.0) as server:
            events = events_of(server.search(1, top_k=top_k))

        def shapes(lists: list[list[float]]) -> list[tuple[float, ...]]:
            return [tuple(round(distance, 9) for distance in row) for row in lists]

        streamed = shapes([found_distances(event) for event in of_type(events, "results")])
        from_core = shapes(
            [[row.distance_km for row in snapshot] for snapshot in snapshots(
                headless_matches(database, 1), limit=top_k, direction=Ranking.GREATEST
            )]
        )
        assert streamed == from_core, (
            "a watchable stream and a headless run must be the same list, seen at the same "
            "moments, or the browser is a second implementation of the ranking"
        )
        assert len(streamed) == done_of(events)["improvements"], (
            "with coalescing switched off, one event per improvement is what is left to show"
        )

    def test_the_search_id_in_the_events_is_the_one_the_registry_lists(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "abelard", 44.0, -93.0, population=50_000),
                dump_row(2, "abelurd", 39.0, -89.0, population=40_000),
            ],
        )

        with serve(database) as server:
            events = events_of(server.search(1))
            listed = server.get("/v1/searches").json()

        assert listed["recent"][0]["search_id"] == started_of(events)["search_id"]
        assert listed["recent"][0]["request"]["top_k"] == DEFAULT_TOP_K
        assert listed["recent"][0]["request"]["direction"] == "greatest"


class TestCoalescingImprovements:
    """The stream reports the best list a reader can use, not one line per change."""

    def test_many_improvements_arrive_as_fewer_events(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=8, per_family=5)

        # The interval is the only thing slowing the events down: the scan itself is stepped
        # a pair at a time, so the ranking changes dozens of times between two writes.
        with serve(database, results_interval=0.2, **LIVELY) as server:
            events = events_of(server.search(1))

        results = of_type(events, "results")
        improvements = done_of(events)["improvements"]
        assert improvements > 3 * len(results), (
            f"{improvements} improvements streamed as {len(results)} events: nothing was "
            "coalesced"
        )
        assert results[-1]["improvements"] == improvements, (
            "the last event must include every improvement, including the ones never sent"
        )
        seen = [event["improvements"] for event in results]
        assert seen == sorted(set(seen)), f"events must advance, not repeat: {seen}"

    def test_a_later_event_holds_every_pair_the_earlier_one_had_plus_the_improvements(
        self, tmp_path: Path
    ) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=7, per_family=5)

        with serve(database, results_interval=0.05, **LIVELY) as server:
            events = events_of(server.search(1, top_k=5))

        results = of_type(events, "results")
        assert len(results) >= 2, f"coalescing over {len(results)} event(s) proves nothing"
        for earlier, later in zip(results, results[1:]):
            assert found_distances(later)[0] >= found_distances(earlier)[0]
            assert later["seen_pairs"] >= earlier["seen_pairs"], (
                "an event never reports having seen fewer pairs than the one before it"
            )


class TestProgressEvents:
    def test_progress_ticks_along_even_when_nothing_has_been_found(self, tmp_path: Path) -> None:
        database = silent_database(tmp_path / "places.sqlite3", places=400)

        with serve(database, progress_interval=0.0, **LIVELY) as server:
            events = events_of(server.search(1))

        progress = of_type(events, "progress")
        assert len(progress) > 1, "a search that finds nothing still has to look alive"
        assert not of_type(events, "results"), "a search with no pairs must not invent any"
        compared = [event["pairs_compared"] for event in progress]
        assert compared == sorted(set(compared)), f"progress went backwards: {compared}"
        assert compared[-1] == done_of(events)["pairs_compared"], (
            "the last tick is the whole scan: progress and totals cannot disagree"
        )
        assert progress[0]["elapsed_seconds"] <= progress[-1]["elapsed_seconds"]

    def test_a_progress_event_carries_the_fraction_and_the_elapsed_time(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=5, per_family=4)

        with serve(database, progress_interval=0.0) as server:
            events = events_of(server.search(1))

        started = started_of(events)
        for event in of_type(events, "progress"):
            assert event["candidate_pairs"] == started["candidate_pairs"], (
                "the denominator is fixed at the start, so a client can draw a progress bar"
            )
            assert 0.0 <= event["share_compared"] <= 1.0
            assert event["elapsed_seconds"] >= 0.0
            assert event["pairs"] <= done_of(events)["pairs"]
        assert of_type(events, "progress")[-1]["share_compared"] == 1.0


class TestWhatTheSearchWillCost:
    def test_the_cost_of_a_search_is_answerable_before_running_it(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=6, per_family=4)

        with serve(database) as server:
            response = server.estimate(1)
            health = server.get("/healthz").json()
            listed = server.get("/v1/searches").json()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["candidate_pairs"] > 0, "a search with no work in it is not a costed search"
        assert body["places"] == 48
        assert body["dataset"] is None, "no dataset was named, so the whole cache is costed"
        assert body["dataset_label"] == "every place in the database"
        assert body["database"] == str(database)
        assert "source" not in body
        assert body["population_floor"] == 99_952, (
            "nobody asked for a floor, so the floor this search ran at is the smallest place in "
            "the cache rather than a filter that was never asked for"
        )
        assert body["estimate"]["comparisons_per_second"] is None, (
            "nothing has been timed on this server yet, so no rate is quoted"
        )
        assert body["estimate"]["seconds"] is None
        assert body["estimate"]["basis"] == "not measured on this machine yet"
        assert health["searches"]["live"] == 0
        assert listed == {"live": [], "recent": []}, (
            "pricing a search must not start one, or leave a record of one behind"
        )

    def test_the_estimate_and_the_search_count_the_same_work(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=5, per_family=4)
        # A dataset is part of the promise, not a label stuck on it: the count quoted for
        # `cities of 99,970 or more people` has to be the count that scan then did, and the
        # count the same request does with no server in the way.
        params = {"min_population": 99_940, "dataset": "99970", "top_k": 1_000}

        with serve(database) as server:
            estimated = server.estimate(1, **params).json()
            events = events_of(server.search(1, **params))

        started, done = started_of(events), done_of(events)
        assert estimated["dataset"] == 99_970
        assert estimated["dataset_label"] == "cities of 99,970 or more people"
        assert estimated["population_floor"] == 99_970, (
            "a dataset and a floor are both floors, so the stricter of the two is the one that "
            "ran and neither one trims the results"
        )
        assert estimated["candidate_pairs"] == started["candidate_pairs"]
        assert estimated["candidate_pairs"] == done["candidate_pairs"]
        assert done["pairs_compared"] == done["candidate_pairs"], (
            "the promise is about the scan that then ran: compared is what was promised"
        )

        headless = open_search(
            SearchRequest(
                n=1, dataset="99970", min_population=99_940, database=database, top_k=1_000
            )
        )
        pairs = list(headless.pairs)
        assert headless.stats.candidate_pairs == estimated["candidate_pairs"], (
            "the price quoted over HTTP is not the work the same request does headless"
        )
        rows = of_type(events, "results")[-1]["results"]
        assert {frozenset((row["first"]["asciiname"], row["second"]["asciiname"])) for row in rows} == {
            frozenset((pair.first.asciiname, pair.second.asciiname)) for pair in pairs
        }, "streamed and headless runs of one request report the same pairs"

    def test_seconds_are_quoted_once_the_server_has_timed_a_scan(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=6, per_family=4)

        with serve(database) as server:
            before = server.estimate(1).json()["estimate"]
            first = done_of(events_of(server.search(1)))
            after = server.estimate(1).json()["estimate"]

        assert before["seconds"] is None
        assert first["comparisons_per_second"] is not None, (
            "a finished search knows what its scanning cost per second"
        )
        assert after["seconds"] is not None, "a measured rate must be used, not left unused"
        assert after["basis"] == "measured from an earlier search on this server"
        assert after["comparisons_per_second"] == pytest.approx(
            first["comparisons_per_second"], rel=0.01
        ), "the estimate and the search that measured it must agree on the rate"

    def test_a_priced_search_can_be_refused_for_the_same_reasons_as_a_running_one(
        self, tmp_path: Path
    ) -> None:
        """`/v1/estimate` and `/v1/search` refuse a request the same way, code for code."""
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            bigger_than_the_places = server.estimate(1, dataset="200000")
            not_a_dataset = server.estimate(1, dataset="nowhere")
            bad_n = server.estimate(-1)
            bad_list = server.estimate(1, top_k=0)
            old_parameter = server.estimate(1, source="database")
            listed = server.get("/v1/searches").json()

        assert bigger_than_the_places.status_code == 404, bigger_than_the_places.text
        assert not_a_dataset.status_code == 422, not_a_dataset.text
        assert bad_n.status_code == 422
        assert bad_list.status_code == 422
        assert old_parameter.status_code == 410, old_parameter.text
        assert listed == {"live": [], "recent": []}, "a refused search became no record"


class TestThePlaceDatabaseIsTheOnlySource:
    """There is nothing to choose any more, so this pins what happens when the cache is wrong."""

    def test_sending_the_removed_source_parameter_is_answered_with_where_it_went(
        self, tmp_path: Path
    ) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.search(1, source="database")
            estimate = server.estimate(1, source="dump")
            listed = server.get("/v1/searches").json()

        assert response.status_code == 410, response.text
        assert response.json()["error"] == "RemovedParameter"
        assert "dataset" in response.json()["message"]
        assert "/v1/datasets" in response.json()["message"], (
            "the refusal says where the choice lives now"
        )
        assert estimate.status_code == 410, "the cost endpoint refuses the same parameter"
        assert listed == {"live": [], "recent": []}, "a refused search never became a record"

    def test_a_source_value_that_is_not_a_source_is_refused_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """The value is not checked, because no value is accepted: the parameter is gone."""
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.search(1, source="spreadsheet")

        assert response.status_code == 410, response.text
        assert response.json()["error"] == "RemovedParameter"

    def test_a_database_that_was_never_imported_is_refused_with_what_to_do(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "places.sqlite3"

        with serve(missing) as server:
            response = server.search(1)

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"] == "CacheNotImported"
        assert "distnsimilarity-import" in body["message"], (
            f"the answer has to say what to run: {body['message']}"
        )
        assert str(missing) in body["message"], "and which database it looked for"

    def test_a_database_with_no_places_in_it_is_refused_the_same_way(
        self, tmp_path: Path
    ) -> None:
        """An imported-but-empty cache is not a search source either."""
        empty = place_database(tmp_path / "places.sqlite3", [])

        with serve(empty) as server:
            response = server.search(1)

        assert response.status_code == 409, response.text
        assert response.json()["error"] == "CacheNotImported"
        assert "distnsimilarity-import" in response.json()["message"]

    def test_a_dataset_bigger_than_every_place_names_the_choices_that_exist(
        self, tmp_path: Path
    ) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "abelard", 44.0, -93.0, population=50_000),
                dump_row(2, "abelurd", 39.0, -89.0, population=40_000),
                dump_row(3, "village", 40.0, -90.0, population=3_000),
            ],
        )

        with serve(database) as server:
            response = server.search(1, dataset="5000000")
            listed = server.get("/v1/searches").json()

        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"] == "DatasetNotAvailable"
        assert "every place in the database" in body["message"], (
            f"the refusal lists the datasets this cache offers: {body['message']}"
        )
        assert "cities of 5,000 or more people" in body["message"], (
            "the third place is under 5,000, so that is where a second choice begins"
        )
        assert listed == {"live": [], "recent": []}



    def test_a_dataset_that_is_not_a_threshold_is_refused_with_what_a_dataset_is(
        self, tmp_path: Path
    ) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            a_word = server.search(1, dataset="big")
            a_file_path = server.search(1, dataset=str(tmp_path / "cities3.txt"))
            listed = server.get("/v1/searches").json()

        for response in (a_word, a_file_path):
            assert response.status_code == 422, response.text
            assert response.json()["error"] == "InvalidSearchRequest"
            assert "dataset" in response.json()["message"]
        assert "file path" in a_file_path.json()["message"], (
            "a search reads the cache, so naming a file is the importer's business"
        )
        assert listed == {"live": [], "recent": []}


class TestOneSearchAtATime:
    def test_a_second_search_is_refused_with_a_reason_and_a_way_out(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as live:
                # The iterator is held for as long as the stream is meant to stay open: dropping
                # it closes the response, which the server reads as a hang-up.
                lines = live.iter_lines()
                first_line = json.loads(next(lines))
                assert first_line["type"] == "started"

                refused = server.search(1)
                assert refused.status_code == 409, refused.text
                body = refused.json()
                assert body["error"] == "SearchAlreadyRunning"
                assert first_line["search_id"] in body["message"], (
                    "the refusal has to name the search that is in the way"
                )
                assert first_line["dataset_label"] in body["message"], (
                    "and name what it is searching in the words the dataset is chosen by, so the "
                    f"person deciding whether to wait knows what they are waiting for: {body['message']}"
                )
                assert "stop" in body["message"] or "close" in body["message"].lower(), (
                    f"the refusal must say what to do about it: {body['message']}"
                )
                assert body["running"]["search_id"] == first_line["search_id"]
                assert server.get("/healthz").json()["searches"]["live"] == 1, (
                    "a refused search must not become a second running search"
                )

    def test_the_next_search_is_welcome_once_the_running_one_has_ended(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                lines = r.iter_lines()
                started = json.loads(next(lines))
                assert started["type"] == "started"

            # Hanging up is the only way to stop it from here, so this is also the test that the
            # refusal above does not lock the server out of its own next search.
            server.wait_for(
                "the abandoned search to be cancelled",
                lambda: server.get("/healthz").json()["searches"]["live"] == 0,
            )
            second = server.search(1)

        assert second.status_code == 200, second.text
        assert started_of(events_of(second))["search_id"] != started["search_id"]

    def test_the_rest_of_the_server_answers_while_a_search_holds_the_scan(
        self, tmp_path: Path
    ) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                events = []
                for event in parse_events(r.iter_lines()):
                    events.append(event)
                    if event["type"] == "results":
                        search_id = started_of(events)["search_id"]
                        listed = server.get("/v1/searches").json()
                        one = server.get(f"/v1/searches/{search_id}").json()
                        cost = server.estimate(1).json()
                        break

        assert [record["search_id"] for record in listed["live"]] == [search_id]
        assert one["live"] is True and one["cancelled"] is False
        assert one["pairs"] >= 1, "the planted pair sits at the head of the database, so it is found first"
        assert one["share_compared"] is not None and 0.0 <= one["share_compared"] < 1.0
        assert cost["candidate_pairs"] == started_of(events)["candidate_pairs"], (
            "pricing another search while one runs is a read, not a second scan"
        )


class TestRefusingABadRequest:
    """Nothing here opens a stream, so nothing here can leak a running search."""

    @pytest.mark.parametrize(
        ("given", "reason"),
        [
            ({"n": -1}, "a negative character difference matches nothing"),
            ({"min_population": -1}, "a negative population floor cannot be a floor"),
            ({"top_k": 0}, "a search that keeps nothing is not a search"),
            ({"top_k": -5}, "a negative list size cannot mean a list"),
            ({"direction": "sideways"}, "a ranking the core does not implement"),
            ({"direction": ""}, "an empty ranking"),
        ],
    )
    def test_a_parameter_the_core_would_refuse_is_refused_as_a_422(
        self, tmp_path: Path, given: dict[str, object], reason: str
    ) -> None:
        database = place_database(
            tmp_path / "places.sqlite3",
            [dump_row(1, "abelard", 44.0, -93.0), dump_row(2, "abelurd", 39.0, -89.0)],
        )

        with serve(database) as server:
            response = server.search(**{"n": 1, **given})
            listed = server.get("/v1/searches").json()

        assert response.status_code == 422, f"{given} should have been refused: {response.text}"
        assert response.json()["error"] == "InvalidSearchRequest", reason
        assert listed == {"live": [], "recent": []}, "a refused search never became a record"

    def test_a_missing_character_difference_is_a_422_from_the_request_model(
        self, tmp_path: Path
    ) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.get("/v1/search")

        assert response.status_code == 422, response.text

    def test_a_place_row_that_is_not_a_place_stops_the_search_naming_the_row(
        self, tmp_path: Path
    ) -> None:
        """A cache can be hand-edited or half-written; a row that is not a place is not searched.

        It cannot be a status code: the rows are read while the scan runs, so the search has
        already opened by the time the bad row is reached. What it can be is an end to the stream
        that names the row, and no pairs. The latitude is broken rather than the name, because
        the name is what the candidate index reads and a bad name would only ever be odd.
        """
        database = pair_database(tmp_path / "places.sqlite3")
        stored = sqlite3.connect(database)
        stored.execute("UPDATE place SET latitude = 400.0 WHERE geonameid = 2")
        stored.commit()
        stored.close()

        with serve(database) as server:
            response = server.search(1)
            events = events_of(response)
            listed = server.get("/v1/searches").json()

        assert of_type(events, "error"), "a corrupt row ended the stream with nothing to show"
        error = events[-1]
        assert error["message"].startswith("PlaceRowError"), error["message"]
        assert "place 2" in error["message"], (
            f"the message has to say which row is broken: {error['message']}"
        )
        assert not of_type(events, "results"), "a search that cannot read its places finds no pairs"
        record = listed["recent"][0]
        assert record["completed"] is False and record["pairs"] == 0

    def test_an_unknown_search_id_is_a_404(self, tmp_path: Path) -> None:
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            response = server.get("/v1/searches/nosuchsearch")

        assert response.status_code == 404, response.text
        assert response.json()["error"] == "UnknownSearch"


class TestHangingUpStopsTheWork:
    """A client that stops reading has stopped the search, not just its own view of it."""

    def test_a_client_that_stops_reading_cancels_the_search(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                events = []
                for event in parse_events(r.iter_lines()):
                    events.append(event)
                    if event["type"] == "results":
                        break

            search_id = started_of(events)["search_id"]
            server.wait_for(
                f"the abandoned search {search_id} to be cancelled",
                lambda: (
                    (record := server.registry.get(search_id)) is not None
                    and record.cancelled
                ),
            )
            record = server.record(search_id)
            assert record.live is False, "a cancelled search is no longer a live one"
            assert record.completed is False, (
                "a search nobody was reading is not a search that finished"
            )
            assert "stopped reading" in (record.cancel_reason or ""), record.cancel_reason
            assert server.get("/healthz").json()["searches"]["live"] == 0

    def test_a_client_that_never_reads_anything_still_stops_the_search(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server:
            client = server.client()
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                search_id = r.headers["X-Search-Id"]
                server.wait_for(
                    "the search to be registered before the client goes away",
                    lambda: server.registry.get(search_id) is not None,
                )
            client.close()

            server.wait_for(
                f"the unread search {search_id} to be cancelled",
                lambda: (
                    (record := server.registry.get(search_id)) is not None
                    and record.cancelled
                ),
            )

        record = server.record(search_id)
        assert record.cancelled, "a search nobody ever read must not be left running"
        assert not record.completed, "an unread search stops short of the end of its candidates"
        assert record.stats.share_compared() is not None
        assert record.stats.share_compared() < 0.5, (
            "the point of noticing a client that never read anything is that the work it had "
            f"not done never happens, and this scan got through {record.stats.share_compared():.0%}"
        )

    def test_the_work_really_stops_rather_than_running_to_the_end(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=1200)
        whole_search = open_search(SearchRequest(n=1, database=database))
        total_comparisons = 0
        for _ in whole_search.pairs:
            pass
        total_polls = whole_search.control.polls
        assert total_polls > 20, (
            f"a search this small ({total_polls} polls) cannot show work being stopped"
        )

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                events = []
                for event in parse_events(r.iter_lines()):
                    events.append(event)
                    if event["type"] == "results":
                        break

            search_id = started_of(events)["search_id"]
            server.wait_for(
                "the cancelled search to stop polling",
                lambda: (
                    (record := server.registry.get(search_id)) is not None
                    and record.cancelled
                    and record.stats.comparison_polls > 0
                ),
            )
            polls_at_first_look = server.record(search_id).stats.comparison_polls

        assert polls_at_first_look < total_polls, (
            f"the abandoned search polled {polls_at_first_look} times and the whole scan is "
            f"{total_polls}: hanging up changed nothing about the work"
        )
        time.sleep(0.2)
        assert server.record(search_id).stats.comparison_polls == polls_at_first_look, (
            "the polling count is still climbing after the search was cancelled"
        )

    def test_a_cancelled_search_is_written_up_as_cancelled(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                events = []
                for event in parse_events(r.iter_lines()):
                    events.append(event)
                    if event["type"] == "results":
                        break
            search_id = started_of(events)["search_id"]
            server.wait_for(
                "the abandoned search to be listed as recent",
                lambda: [record["search_id"] for record in server.get("/v1/searches").json()["recent"]]
                == [search_id],
            )
            listed = server.get(f"/v1/searches/{search_id}").json()

        assert listed["live"] is False
        assert listed["completed"] is False
        assert listed["cancelled"] is True
        assert listed["share_compared"] is not None and listed["share_compared"] < 1.0, (
            "a search stopped early has not compared everything it was going to"
        )
        assert listed["comparisons_per_second"] is None, (
            "a cancelled search is not a measurement of what a search costs"
        )

    def test_a_cancelled_search_is_the_one_worth_running_again(self, tmp_path: Path) -> None:
        database = noisy_database(tmp_path / "places.sqlite3", places=SLOW_PLACES)

        with serve(database, **LIVELY) as server, server.client() as client:
            with client.stream("GET", "/v1/search", params={"n": 1}) as r:
                search_id = r.headers["X-Search-Id"]
                for _ in parse_events(r.iter_lines()):
                    break  # one event, then hang up
            server.wait_for(
                "the abandoned search to be listed as retryable",
                lambda: [
                    row["retryable"]
                    for row in server.get("/v1/searches").json()["recent"]
                    if row["search_id"] == search_id
                ]
                == [True],
            )
            cancelled = server.get(f"/v1/searches/{search_id}").json()
            # The scan is free now, so the same request can be run to the end.
            finished = done_of(events_of(server.search(1)))

        assert cancelled["retryable"] is True, (
            "a search stopped with work undone is a search worth running again"
        )
        assert finished["retryable"] is False, "a search that finished has nothing to retry"
        assert finished["cancelled"] is False


class TestWatchingFromATerminal:
    """`distnsimilarity-watch`: the same stream, printed as it arrives.

    The ticket says this is how the streaming is verified - no browser, just a client reading
    newline-delimited JSON. What is asserted here is that the client prints a list, and then
    prints a better one, while the same search is still going; a client that waited for `done`
    would print the same rows and mean something different.
    """

    def test_a_terminal_watches_the_best_list_improve(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=6, per_family=4)
        printed = io.StringIO()

        with serve(database, **LIVELY) as server:
            # No `--min-population` here on purpose: an option nobody set must not go out empty
            # and have the search refused over it.
            code = watch_cli.main(
                ["--url", server.base_url, "--n", "1", "--rows", "3"],
                out=printed,
            )

        output = printed.getvalue()
        assert code == 0, output
        lines = output.splitlines()
        assert any("pairs to compare" in line for line in lines), (
            "the client says what the search will cost before it says anything about finds"
        )
        headers = [index for index, line in enumerate(lines) if line.startswith("  best ")]
        assert len(headers) > 1, (
            "a watcher that printed the list once watched a result, not a search"
        )
        first_rows = [lines[index + 1] for index in headers]
        assert len(set(first_rows)) > 1, f"the top row never improved: {first_rows}"
        assert any("% of" in line for line in lines), "progress is printed unless asked not to be"
        assert any(
            line.startswith("search ") and "completed" in line for line in lines
        ), "the client sums the search up at the end"

    def test_a_terminal_can_watch_the_lists_without_the_progress_lines(self, tmp_path: Path) -> None:
        database = many_pair_database(tmp_path / "places.sqlite3", families=3, per_family=2)
        printed = io.StringIO()

        with serve(database) as server:
            code = watch_cli.main(
                ["--url", server.base_url, "--n", "1", "--no-progress"],
                out=printed,
            )

        output = printed.getvalue()
        assert code == 0, output
        assert "% of" not in output, f"--no-progress still printed a tick: {output}"
        assert "best greatest distances" in output

    def test_everything_the_terminal_asks_for_is_something_the_server_takes(
        self, tmp_path: Path
    ) -> None:
        """The two ends of the stream cannot drift apart on the name of a parameter."""
        database = pair_database(tmp_path / "places.sqlite3")

        with serve(database) as server:
            schema = server.get("/openapi.json").json()

        accepted = {
            parameter["name"]
            for path, operations in schema["paths"].items()
            if path.endswith("/v1/search")
            for operation in operations.values()
            for parameter in operation.get("parameters", [])
        }
        sent = {
            name
            for name in vars(
                watch_cli.build_parser().parse_args(["--dataset", "15000", "--n", "1"])
            )
            # Not search parameters: where to connect, and how much of the list to print.
            if name not in {"url", "rows", "no_progress"}
        }
        assert sent <= accepted, (
            f"the client sends {sorted(sent - accepted)}, which is not in the API"
        )
