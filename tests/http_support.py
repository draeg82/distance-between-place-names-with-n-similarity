"""A search server on a real port, and the events a client reads back from it.

Shared by everything that talks HTTP - the streaming contract (#6), the browser front end (#7),
and whatever hangs off the API next - so that "the same search over the wire" means one harness
rather than three that drift. `serve` starts the application the command line starts, on
127.0.0.1, on a port the OS picks; the helpers at the bottom parse the newline-delimited JSON it
writes. The suite stays offline: `tests/conftest.py` allows loopback and refuses anything else.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
import uvicorn

from distnsimilarity.api.app import create_app
from distnsimilarity.api.registry import SearchRecord, SearchRegistry

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
