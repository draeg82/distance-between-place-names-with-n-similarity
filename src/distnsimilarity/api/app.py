"""The HTTP adapter: the search seam with a URL in front of it.

A search is streamed as newline-delimited JSON - `started`, then `progress` and `results` as
the scan runs, then `done` - so a caller sees the best finds improving while the scan is still
running. The ranking in those `results` events is the core's own, and its order is the order a
headless run ranks the same pairs in, so the two adapters are the same search
(`tests/test_http_search.py` checks that event for event).

Everything is searched out of the place database - there is no `source` to choose, and a request
that still sends one is refused rather than quietly ignored - and what a caller chooses instead
is a dataset: a population threshold, listed by `/v1/datasets`.

One scan runs per process at a time, and a second request is refused rather than queued: this
is a tool for one person's laptop, where a second scan would only make the first one slower and
the machine louder. The refusal says which search is in the way and how to move it.

Errors that can be known before a search starts are answered with a status code and no stream
at all: nothing is started, and nothing is left running to be cancelled.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from distnsimilarity.api.frontend import mount_frontend
from distnsimilarity.api.payload import (
    DEFAULT_RESULTS_INTERVAL,
    NDJSON_MEDIA_TYPE,
    asking_words,
    done_line,
    error_line,
    origin_payload,
    place_payload,
    progress_line,
    results_line,
    started_line,
)
from distnsimilarity.api.registry import SearchRecord, SearchRegistry
from distnsimilarity.api.runner import (
    DEFAULT_SEARCH_WORKERS,
    DEFAULT_STEP_SECONDS,
    SearchPool,
    SearchRunner,
)
from distnsimilarity.export.csv_export import LegacyCsvWriter
from distnsimilarity.search import build_metadata
from distnsimilarity.core.place_lookup import (
    InvalidLookupText,
    PlaceNotFound,
    lookup_text,
)
from distnsimilarity.core.sweep import DEFAULT_DIFFERENCE_LIMIT, DifferenceRange
from distnsimilarity.core.topk import DEFAULT_TOP_K, parse_direction
from distnsimilarity.datasets import DatasetNotAvailableError
from distnsimilarity.db.candidates import dataset_options
from distnsimilarity.db.place_lookup import places_matching_name
from distnsimilarity.db.schema import (
    DEFAULT_DATABASE_NAME,
    DatabaseNotImportedError,
    DatabaseSchemaError,
    default_database_path,
)
from distnsimilarity.search import (
    DEFAULT_BATCH_SIZE,
    CacheNotImportedError,
    InvalidSearchRequest,
    SearchRequest,
    SearchSession,
    open_search,
)

#: How often a `progress` event may be sent. It carries no results, only how far the scan has
#: got, so it is slower than `results` on purpose: a client watching a list does not need the
#: denominator twenty times a second.
DEFAULT_PROGRESS_INTERVAL = 0.5

#: The refusal a client is allowed to branch on, one per row, with the status that carries it.
#: These codes are the wire contract and are written out here rather than derived from the
#: Python class, because a client must keep working when a module is renamed and a class is
#: split in two. Every one of these is decided before the first pair, so none of them arrives
#: as a half-open stream.
REQUEST_ERRORS: tuple[tuple[tuple[type[BaseException], ...], int, str], ...] = (
    ((InvalidSearchRequest,), 422, "InvalidSearchRequest"),
    ((InvalidLookupText,), 422, "InvalidPlaceLookup"),
    ((PlaceNotFound,), 404, "PlaceNotFound"),
    ((DatasetNotAvailableError,), 404, "DatasetNotAvailable"),
    ((CacheNotImportedError,), 409, "CacheNotImported"),
    ((DatabaseNotImportedError,), 409, "PlaceDatabaseNotImported"),
    ((DatabaseSchemaError,), 409, "PlaceDatabaseUnusable"),
)

#: A row in the place table that is not a place (`PlaceRowError`) is deliberately absent: rows are
#: read while the scan runs, so it can only arrive after `started`, as an `error` event naming the
#: row. Promising a status code for it would promise a refusal this server cannot make.

#: The two refusals that are about the server's state rather than about the request.
SEARCH_ALREADY_RUNNING = "SearchAlreadyRunning"
UNKNOWN_SEARCH = "UnknownSearch"


#: The parameter that used to choose between a dump file and the place cache. The database is
#: the only search source now, so a caller who still sends it is contradicting themselves about
#: what is being searched; that is refused, where a quietly ignored parameter would leave them
#: reading places they never asked for.
REMOVED_PARAMETER = "source"


def refusal_for_removed_parameter(sent: Mapping[str, object] | None) -> JSONResponse | None:
    """The answer to a request that still asks which source to read, or None if it does not.

    `sent` is whatever the caller sent: the query parameters of a GET, or the JSON body of a
    POST. Both are ways of asking a question this server no longer answers.

    410 rather than 422: the parameter existed, this server stopped serving it, and a client
    still sending it is speaking an older version of this API. That is a different fix from a
    malformed request - change which parameter you send - and the code lets a client see the
    difference without reading the sentence.
    """
    if not sent or REMOVED_PARAMETER not in sent:
        return None
    return JSONResponse(
        {
            "error": "RemovedParameter",
            "message": (
                f"{REMOVED_PARAMETER} is no longer a search parameter: the place database is "
                "the only search source. Choose a dataset instead - a population threshold "
                "(dataset=5000), or leave it out to search every place - and see /v1/datasets."
            ),
        },
        status_code=410,
    )


def error_response(error: BaseException) -> JSONResponse | None:
    """The answer to a search that never started, or None if this is not a known refusal."""
    for types, status, code in REQUEST_ERRORS:
        if isinstance(error, types):
            return JSONResponse({"error": code, "message": str(error)}, status_code=status)
    return None


def refused_because_running(running: SearchRecord) -> JSONResponse:
    """The answer to a second search: no, this one, and why.

    Refusing has to be actionable, so the message names the search in the way, what it is asking
    for, how far it has got, and the one thing that will free the server. `asking_words` is what
    makes that work for either mode: a refusal that quoted `n=None` for a sweep would be quoting a
    parameter the running search never had.
    """
    summary = running.as_dict()
    share = running.stats.share_compared()
    where = "has not started comparing yet" if not share else f"is {share:.0%} compared"
    return JSONResponse(
        {
            "error": SEARCH_ALREADY_RUNNING,
            "message": (
                f"one search runs at a time on this server, and {running.search_id} "
                f"({running.request.get('dataset_label')}, "
                f"{asking_words(running.request)}) {where}, "
                f"{running.seconds}s in. Close the connection that started it to stop it, or "
                f"ask for /v1/searches/{running.search_id} to watch it."
            ),
            "search_id": running.search_id,
            "running": summary,
        },
        status_code=409,
    )


def read_only_connection(path: Path) -> sqlite3.Connection | None:
    """A connection for listing, or None if there is nothing to list.

    Listing what a server can search must not leave an empty database behind, so this opens the
    file read-only instead of going through `db.schema.connect`, which creates the schema.
    """
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def cache_status(path: Path) -> dict[str, Any]:
    """The place database a search would read: where it is, and whether it has been imported.

    `imported` is the answer a search depends on - a cache that is present but empty refuses
    every request - so `/healthz` can say it before anybody tries.
    """
    connection = read_only_connection(path)
    if connection is None:
        return {"path": str(path), "present": path.is_file(), "places": 0, "imported": False}
    try:
        places = int(connection.execute("SELECT COUNT(*) FROM place").fetchone()[0])
    except sqlite3.Error:
        places = 0
    finally:
        connection.close()
    return {"path": str(path), "present": True, "places": places, "imported": places > 0}


def create_app(
    *,
    data_dir: Path | None = None,
    database: Path | None = None,
    search_workers: int = DEFAULT_SEARCH_WORKERS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    step_seconds: float = DEFAULT_STEP_SECONDS,
    results_interval: float = DEFAULT_RESULTS_INTERVAL,
    progress_interval: float = DEFAULT_PROGRESS_INTERVAL,
    registry: SearchRegistry | None = None,
) -> FastAPI:
    """Build the API. `data_dir` and `database` default to the ones the command line uses.

    `registry` and the search pool hang off `app.state`, so a caller that wants to cancel a
    search from outside a request - a control endpoint, a test - has the same objects the
    requests are using.
    """
    searches = SearchRegistry() if registry is None else registry
    pool = SearchPool(
        workers=search_workers, batch_size=batch_size, step_seconds=step_seconds
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        pool.close()

    app = FastAPI(
        title="DistNSimilarity search API",
        description="Pairs of places whose ASCII names differ by exactly n characters.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.registry = searches
    app.state.pool = pool
    app.state.data_dir = None if data_dir is None else Path(data_dir)
    app.state.database = None if database is None else Path(database)
    # The browser adapter is served by the process that answers it, so one command starts both
    # and the page cannot be looking at a different place database than the search it runs.
    mount_frontend(app)

    def cache_path() -> Path:
        """The database this server searches.

        `--database` first, then `places.sqlite3` in the data directory it was given, then the
        tool's own default - `DISTNSIMILARITY_DB`, or the cache beside the dumps. The same
        answer the command line would come to, because a server and a terminal reading different
        caches would report different distances for the same request.
        """
        database: Path | None = app.state.database
        if database is not None:
            return database
        data_dir: Path | None = app.state.data_dir
        return data_dir / DEFAULT_DATABASE_NAME if data_dir is not None else default_database_path()

    def build_request(
        *,
        dataset: str | None,
        n: int | None,
        min_population: int | None,
        full_comparison: bool,
        top_k: int,
        direction: str,
        from_place: int | None = None,
        difference_limit: int | None = None,
        exclude_identical_names: bool = False,
    ) -> tuple[SearchRequest, dict[str, Any]]:
        """The seam's request plus the echo an event carries, or the refusal of both.

        One place builds a request for every endpoint, so `/v1/estimate` cannot accept a
        parameter that `/v1/search` would then refuse - including the two mode parameters, which
        the seam refuses or accepts in the same way for both.
        """
        # The direction is read here rather than left to `SearchRequest`, which takes a
        # `Ranking`: a spelling the core does not implement is a parameter the client has to
        # be told about, not a server error on the way to building the request.
        try:
            ranking = parse_direction(direction)
        except ValueError as error:
            raise InvalidSearchRequest(str(error)) from error
        # A range is only built when there is something to build it for: a limit nobody gave stays
        # nobody gave it rather than turning into a default the caller never asked for. A range
        # parameter that arrives without a place to sweep from is handed to the seam anyway, which
        # refuses it - a query string asking for two different searches is not a query string with
        # one parameter to quietly drop.
        asks_for_a_range = (
            from_place is not None
            or difference_limit is not None
            or exclude_identical_names
        )
        try:
            difference = (
                None
                if not asks_for_a_range
                else DifferenceRange(
                    limit=(
                        DEFAULT_DIFFERENCE_LIMIT
                        if difference_limit is None
                        else difference_limit
                    ),
                    exclude_identical_names=exclude_identical_names,
                )
            )
        except ValueError as error:
            # "-1 differing characters" is a bad parameter, not a broken server: the range says so
            # in words the same 422 carries for every other bad parameter here.
            raise InvalidSearchRequest(str(error)) from error
        search_request = SearchRequest(
            n=n,
            dataset=dataset,
            min_population=min_population,
            equal_length_prefilter=not full_comparison,
            database=cache_path(),
            top_k=top_k,
            direction=ranking,
            from_place=from_place,
            difference=difference,
        )
        echo = {
            "requested_dataset": dataset,
            # The number a search filters on (null for the whole cache) and the words for it: a
            # client filters on one and shows the other, and neither is a file name.
            "dataset": search_request.dataset_threshold,
            "dataset_label": search_request.dataset_name,
            "n": search_request.n,
            "min_population": min_population,
            "equal_length_prefilter": search_request.equal_length_prefilter,
            "database": str(search_request.database_path),
            "top_k": search_request.top_k,
            "direction": search_request.direction.value,
            # Which of the two searches this is, and the parameters that belong to it. A client
            # branches on `mode` rather than on `from_place is not null`, so adding a third search
            # later is a new word in one field rather than a new null to test for everywhere.
            "mode": search_request.mode,
            "from_place": search_request.from_place,
            "difference_limit": (
                None if search_request.difference is None else search_request.difference.limit
            ),
            "exclude_identical_names": (
                None
                if search_request.difference is None
                else search_request.difference.exclude_identical_names
            ),
            # Filled in when the place database has answered for the id: `from_place` is what the
            # caller sent, `origin` is the place it turned out to mean.
            "origin": None,
        }
        return search_request, echo

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        """Alive, how much searching it is doing right now, and what it has to search."""
        return {
            "status": "ok",
            "searches": {"live": searches.live_count},
            "database": cache_status(cache_path()),
        }

    @app.get("/v1/datasets")
    async def datasets() -> dict[str, Any]:
        """The dataset choice: the thresholds this database can be searched at, in words.

        Options come from the places actually in the table, each with the label to show, the
        places it holds and the pairs a search there would compare, so a dropdown cannot offer
        a dataset this server would search to nothing. The database itself is reported with
        them, because an empty one offers nothing and says why.
        """
        path = cache_path()
        connection = read_only_connection(path)
        if connection is None:
            return {"database": cache_status(path), "datasets": []}
        try:
            options = dataset_options(connection)
        except sqlite3.Error:
            return {"database": cache_status(path), "datasets": []}
        finally:
            connection.close()
        return {
            "database": cache_status(path),
            "datasets": [option.as_dict() for option in options],
        }

    @app.get("/v1/places")
    async def places(
        request: Request,
        name: str | None = Query(
            None,
            description="an ASCII name, spelled exactly: no prefix, no case fold, two characters "
            "at least",
        ),
    ) -> Response:
        """Every place a typed name is a question about, biggest first: the choices to pick from.

        A name is not a place. `Springfield` belongs to more than a hundred places in the
        cities1000 cache, in several countries, and the distance depends on which one a search
        starts from - so the answer is the whole list, with the country, population and
        coordinates a person needs to tell them apart, and the `geonameid` the search is then run
        from. Nothing here chooses for them: the first row is the biggest, not the intended.

        "There is no place database" is refused with a status code rather than answered with an
        empty list, because an empty list is the page's other answer - "no place is called that" -
        and the two are different things to tell a person.
        """
        refused = refusal_for_removed_parameter(request.query_params)
        if refused is not None:
            return refused
        if name is None:
            return JSONResponse(
                {
                    "error": "InvalidPlaceLookup",
                    "message": (
                        "a place lookup asks for a name: /v1/places?name=Springfield. Two "
                        "characters at least, spelled exactly."
                    ),
                },
                status_code=422,
            )
        path = cache_path()
        connection = read_only_connection(path)
        if connection is None:
            return JSONResponse(
                {
                    "error": "PlaceDatabaseNotImported",
                    "message": (
                        f"there is no place database to look a name up in at {path}: run "
                        "distnsimilarity-import to fill one from a GeoNames dump"
                    ),
                    "database": cache_status(path),
                },
                status_code=409,
            )
        try:
            typed = lookup_text(name)
            found = places_matching_name(connection, typed)
        except ValueError as error:
            connection.close()
            refusal = error_response(error)
            if refusal is None:
                raise
            return refusal
        except sqlite3.Error as error:
            connection.close()
            return JSONResponse(
                {
                    "error": "PlaceDatabaseUnusable",
                    "message": f"the place database at {path} could not be read: {error}",
                    "database": cache_status(path),
                },
                status_code=409,
            )
        connection.close()
        return JSONResponse(
            {
                "database": cache_status(path),
                "name": typed,
                "match_count": len(found),
                "matches": [place_payload(place) for place in found],
            }
        )

    @app.get("/v1/searches")
    async def list_searches() -> dict[str, Any]:
        """The search this process is running, and the last few that ended."""
        return {
            "live": [record.as_dict() for record in searches.live()],
            "recent": [record.as_dict() for record in searches.recent()],
        }

    @app.get("/v1/searches/{search_id}")
    async def one_search(search_id: str) -> Response:
        record = searches.get(search_id)
        if record is None:
            return JSONResponse(
                {"error": UNKNOWN_SEARCH, "message": f"no search {search_id} on this server"},
                status_code=404,
            )
        return JSONResponse(record.as_dict())

    async def open_on_worker(
        search_request: SearchRequest, echo: dict[str, Any]
    ) -> SearchSession:
        """Open a search off the event loop: counting a big candidate set is the slow part.

        A dataset that names no threshold, and a database nobody has imported, are answered
        with a status code, so this is where a bad parameter stops being a request and becomes
        an answer - before a pair exists and before anything needs cancelling. So is a
        `from_place` that is not a place in this database: 404, no stream, nothing to cancel.

        Opening is also where a chosen id becomes a place, so the echo carries it: a client that
        watches a sweep, and a server refusing a second one, both say which place the search
        starts from without asking the database twice.
        """
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(pool.executor, partial(open_search, search_request))
        echo["origin"] = origin_payload(session.origin)
        return session

    @app.get("/v1/estimate")
    async def estimate(
        request: Request,
        dataset: str | None = Query(
            None, description="population threshold (5000) or dump name; omit for every place"
        ),
        n: int | None = Query(
            None,
            description="everywhere mode: exactly this many differing character positions",
        ),
        min_population: int | None = Query(None, description="never compare places below this"),
        full_comparison: bool = Query(False, description="ignore the equal-length prefilter"),
        top_k: int = Query(DEFAULT_TOP_K, description="how many of the best finds to keep"),
        direction: str = Query("greatest", description="greatest or least distance first"),
        from_place: int | None = Query(
            None,
            description="from-a-place mode: the GeoNames id to search outward from "
            "(/v1/places?name=... hands out the ids)",
        ),
        difference_limit: int | None = Query(
            None,
            description="from-a-place mode: sweep from 0 differing characters up to this many "
            f"(default {DEFAULT_DIFFERENCE_LIMIT})",
        ),
        exclude_identical_names: bool = Query(
            False,
            description="from-a-place mode: leave the same-name pairs out of the finds",
        ),
    ) -> Response:
        """What a search would cost, without searching.

        `candidate_pairs` is exact and is what the search will compare; `estimate.seconds` is
        only quoted once something has actually been measured on this machine, and says which
        of the two it is. This is the number a person decides with, so it is not a guess.

        In from-a-place mode `candidate_pairs` is a place's partner set - the candidates holding
        its own name length, itself left out - rather than every pair in the candidate set, which
        is why the same field reads as tens of thousands here and millions in everywhere mode.
        """
        refused = refusal_for_removed_parameter(request.query_params)
        if refused is not None:
            return refused
        try:
            search_request, echo = build_request(
                dataset=dataset,
                n=n,
                min_population=min_population,
                full_comparison=full_comparison,
                top_k=top_k,
                direction=direction,
                from_place=from_place,
                difference_limit=difference_limit,
                exclude_identical_names=exclude_identical_names,
            )
            session = await open_on_worker(search_request, echo)
        except BaseException as error:
            refusal = error_response(error)
            if refusal is None:
                raise
            return refusal
        try:
            cost = searches.rate.estimate(session.stats.candidate_pairs)
            return JSONResponse(
                {
                    **echo,
                    "places": session.stats.places,
                    "population_floor": session.stats.population_floor,
                    "candidate_pairs": session.stats.candidate_pairs,
                    # The values a sweep will report, before it has compared anything: which
                    # numbers a reader is about to see fill up.
                    "differences": session.stats.as_dict()["differences"],
                    "estimate": cost.as_dict(),
                }
            )
        finally:
            # Counting the work is not doing it: no comparison has happened and none will.
            session.close()

    @app.get("/v1/search")
    async def stream_search(
        request: Request,
        dataset: str | None = Query(
            None, description="population threshold (5000) or dump name; omit for every place"
        ),
        n: int | None = Query(
            None,
            description="everywhere mode: exactly this many differing character positions",
        ),
        min_population: int | None = Query(None, description="never compare places below this"),
        full_comparison: bool = Query(False, description="ignore the equal-length prefilter"),
        top_k: int = Query(DEFAULT_TOP_K, description="how many of the best finds to keep"),
        direction: str = Query("greatest", description="greatest or least distance first"),
        from_place: int | None = Query(
            None,
            description="from-a-place mode: the GeoNames id to search outward from "
            "(/v1/places?name=... hands out the ids)",
        ),
        difference_limit: int | None = Query(
            None,
            description="from-a-place mode: sweep from 0 differing characters up to this many "
            f"(default {DEFAULT_DIFFERENCE_LIMIT})",
        ),
        exclude_identical_names: bool = Query(
            False,
            description="from-a-place mode: leave the same-name pairs out of the finds",
        ),
    ) -> Response:
        """Stream a search: `started`, then `progress` and `results` as it runs, then `done`.

        The search's own order decides which pairs have been seen; the core's ranking decides
        which of them are the best. Nothing is held back to be batched beyond the coalescing
        interval on each kind of event.

        Both modes share the stream: a sweep from one place sends the same four events, with the
        place it starts from in `started` and what each value of its range has found in every
        event after that.
        """
        refused = refusal_for_removed_parameter(request.query_params)
        if refused is not None:
            return refused
        try:
            # Both halves of the refusal are decided before a pair exists - building the
            # request rejects a bad `n`, floor, dataset, list size or direction; opening the
            # database reads nothing but counts, and looks the chosen place up by its key - so
            # the answer is a status code, with no stream and nothing to cancel.
            search_request, echo = build_request(
                dataset=dataset,
                n=n,
                min_population=min_population,
                full_comparison=full_comparison,
                top_k=top_k,
                direction=direction,
                from_place=from_place,
                difference_limit=difference_limit,
                exclude_identical_names=exclude_identical_names,
            )
            session = await open_on_worker(search_request, echo)
        except BaseException as error:
            refusal = error_response(error)
            if refusal is None:
                raise
            return refusal

        record, running = searches.claim(session, echo)
        if record is None:
            session.close()
            assert running is not None
            return refused_because_running(running)

        runner = pool.start(session)
        return StreamingResponse(
            _search_events(
                runner,
                record,
                searches,
                echo,
                results_interval=results_interval,
                progress_interval=progress_interval,
            ),
            media_type=NDJSON_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                "X-Search-Id": record.search_id,
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/export")
    async def export(
        request: Request,
        dataset: str | None = Query(
            None, description="population threshold (5000) or dump name; omit for every place"
        ),
        n: int | None = Query(
            None,
            description="everywhere mode: exactly this many differing character positions",
        ),
        min_population: int | None = Query(None, description="never compare places below this"),
        full_comparison: bool = Query(False, description="ignore the equal-length prefilter"),
        top_k: int = Query(DEFAULT_TOP_K, description="how many of the best finds to keep"),
        direction: str = Query("greatest", description="greatest or least distance first"),
        from_place: int | None = Query(
            None,
            description="from-a-place mode: the GeoNames id to search outward from "
            "(/v1/places?name=... hands out the ids)",
        ),
        difference_limit: int | None = Query(
            None,
            description="from-a-place mode: sweep from 0 differing characters up to this many "
            f"(default {DEFAULT_DIFFERENCE_LIMIT})",
        ),
        exclude_identical_names: bool = Query(
            False,
            description="from-a-place mode: leave the same-name pairs out of the finds",
        ),
    ) -> Response:
        """Export results as legacy CSV: place1, lat1, lon1, place2, lat2, lon2, haversinedistance.

        Runs the full search, writes every pair (even zero-result searches get a valid
        file with just the header), and records the ranking direction and parameter values
        in comment lines above the header.

        Diagnostics, progress and errors never land inside the export file.
        Distances are in kilometres, matching what the interface shows.

        This endpoint runs synchronously and may take seconds or minutes for large datasets.
        """
        refused = refusal_for_removed_parameter(request.query_params)
        if refused is not None:
            return refused

        # Run the entire search+export in an executor to avoid blocking the event loop.
        loop = asyncio.get_running_loop()

        def run_export(db: Path) -> bytes:
            """Run the search and export in a background thread."""
            from distnsimilarity.search import SearchRequest

            diff = (
                None
                if from_place is None
                else DifferenceRange(
                    limit=DEFAULT_DIFFERENCE_LIMIT
                    if difference_limit is None
                    else difference_limit,
                    exclude_identical_names=exclude_identical_names,
                )
            )
            ranking = parse_direction(direction)
            try:
                request = SearchRequest(
                    n=n if from_place is None else None,
                    dataset=dataset,
                    min_population=min_population,
                    equal_length_prefilter=not full_comparison,
                    database=db,
                    top_k=top_k,
                    direction=ranking,
                    from_place=from_place,
                    difference=diff,
                )
            except InvalidSearchRequest as error:
                return error_line(f"InvalidSearchRequest: {error}".encode())

            session = open_search(request)
            metadata = build_metadata(session)
            export_path = (
                session.database_path.parent / f"export_{session.stats.started_at:.6f}.csv"
            )
            with LegacyCsvWriter(export_path, metadata=metadata) as writer:
                for match in session.pairs:
                    writer.write(match.first, match.second, match.distance_km)
            session.close()
            csv_bytes = export_path.read_bytes()
            export_path.unlink(missing_ok=True)
            return csv_bytes

        try:
            db = cache_path()
            csv_bytes = await loop.run_in_executor(None, run_export, db)
        except TypeError as error:
            return JSONResponse(
                {"error": "InvalidSearchRequest", "message": str(error)}, status_code=422
            )
        except PlaceNotFound as error:
            return JSONResponse(
                {"error": "PlaceNotFound", "message": str(error)}, status_code=404
            )
        except CacheNotImportedError as error:
            return JSONResponse(
                {"error": "CacheNotImported", "message": str(error)}, status_code=409
            )

        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="distnsimilarity-export.csv"',
            },
        )

    return app


async def _search_events(
    runner: SearchRunner,
    record: SearchRecord,
    searches: SearchRegistry,
    echo: dict[str, Any],
    *,
    results_interval: float,
    progress_interval: float,
) -> AsyncIterator[str]:
    """Write one search to a socket, and stop it if nobody is reading.

    Two clocks run over one scan. Improvements are *coalesced*: the ranking may have changed
    twenty times since the last line, and the client gets the current list once per interval
    instead of once per change. Progress ticks on its own, slower, and regardless of results,
    because a search that has found nothing yet still has to look alive.

    The `finally` is the whole cancellation story: however this generator ends - finished,
    abandoned, or cancelled by the server when the client went away - the search is cancelled,
    its rate is remembered for the next estimate, and the registry is told, without awaiting
    anything that a cancellation could interrupt.
    """
    search_id = record.search_id
    session = runner.session
    stopped_because = "the search ended"
    cost = searches.rate.estimate(record.stats.candidate_pairs)
    try:
        yield started_line(
            search_id=search_id,
            request=echo,
            stats=record.stats.as_dict(),
            estimate=cost.as_dict(),
        )
        last_progress = 0.0
        last_results = 0.0
        improvements_sent = 0
        # No tick has gone out yet, and no scan has compared a negative number of pairs.
        progress_reported = -1
        while True:
            step = await runner.next_step()
            now = time.monotonic()
            stats = record.stats.as_dict()
            if stats["improvements"] != improvements_sent and (
                now - last_results >= results_interval or step.exhausted
            ):
                improvements_sent = int(stats["improvements"])
                last_results = now
                yield results_line(
                    search_id,
                    list(session.snapshot()),
                    direction=session.direction.value,
                    top_k=session.limit,
                    stats=stats,
                )
            # A tick says what has happened since the last one. "Nothing new happened" is the
            # one thing a tick must not say twice, so an unchanged count waits out the interval
            # again instead of going out as a repeat; the search still opens with a tick whether
            # or not there is anything to report yet, and the totals belong in `done`.
            if now - last_progress >= progress_interval and (
                stats["pairs_compared"] != progress_reported
            ):
                last_progress = now
                progress_reported = int(stats["pairs_compared"])
                yield progress_line(search_id, stats)
            if step.finished:
                break
        yield done_line(search_id, record.stats.as_dict())
    except asyncio.CancelledError:
        stopped_because = "the client stopped reading"
        raise
    except BaseException as error:  # a search that fails mid-stream says so on the stream
        stopped_because = "the search failed"
        record.stats.error = f"{type(error).__name__}: {error}"
        yield error_line(f"{type(error).__name__}: {error}", search_id)
    finally:
        runner.cancel(stopped_because)
        searches.finish(record)
