"""Serving the search API: `distnsimilarity-serve --host 127.0.0.1 --port 8000`.

The listening socket is opened here, before the server starts, for two reasons: `--port 0`
means "any free port" and the caller has to be told which one was taken, and a machine that
cannot bind the address fails here with a plain error rather than a traceback from inside
the ASGI server. The port is printed on stdout as soon as it is bound, which is what a
supervisor - or a test - waits for.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from distnsimilarity.api.app import DEFAULT_PROGRESS_INTERVAL, create_app
from distnsimilarity.api.payload import DEFAULT_RESULTS_INTERVAL
from distnsimilarity.api.registry import SearchRegistry
from distnsimilarity.api.runner import DEFAULT_SEARCH_WORKERS, DEFAULT_STEP_SECONDS
from distnsimilarity.datasets import DatasetNotAvailableError, default_data_dir
from distnsimilarity.db.schema import DEFAULT_DATABASE_NAME
from distnsimilarity.search import DEFAULT_BATCH_SIZE

#: The line a supervisor waits for. Keep the prefix stable; the port on it is the point.
LISTENING_PREFIX = "serving on "
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def bind(host: str, port: int, *, backlog: int = 2048) -> socket.socket:
    """Open a listening socket on `host`, with `port=0` meaning any free port."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    family, socktype, protocol, _, address = infos[0]
    listening = socket.socket(family, socktype, protocol)
    listening.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listening.bind(address)
    except OSError as error:
        listening.close()
        raise OSError(f"cannot listen on {host}:{port}: {error}") from error
    listening.listen(backlog)
    return listening


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distnsimilarity-serve",
        description=(
            "Serve the search API: streamed searches over HTTP, the same search the "
            "command line runs."
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Default: {DEFAULT_HOST}.")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Default: {DEFAULT_PORT}. Use 0 for any free port; the port is printed.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Directory holding the GeoNames dumps. Default: the one the CLI finds.",
    )
    parser.add_argument(
        "--database",
        default=None,
        help=(
            f"Place cache used for searches with source=database. "
            f"Default: {DEFAULT_DATABASE_NAME} beside the dumps."
        ),
    )
    parser.add_argument(
        "--search-workers",
        type=int,
        default=DEFAULT_SEARCH_WORKERS,
        help=(
            f"Worker threads shared by the server (default: {DEFAULT_SEARCH_WORKERS}). One "
            "scan runs at a time; the others are for opening a dataset and pricing a search "
            "while that scan holds one."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"Pairs produced per step (default: {DEFAULT_BATCH_SIZE}). Smaller means "
            "a cancelled search stops sooner, at the cost of more handoffs."
        ),
    )
    parser.add_argument(
        "--step-seconds",
        type=float,
        default=DEFAULT_STEP_SECONDS,
        help=(
            f"How long one step may look for pairs before writing what it has "
            f"(default: {DEFAULT_STEP_SECONDS}). This is the worst case wait for a pair "
            "behind a stretch of scan that finds nothing."
        ),
    )
    parser.add_argument(
        "--results-interval",
        type=float,
        default=DEFAULT_RESULTS_INTERVAL,
        help=(
            f"Shortest gap between two top-K events, in seconds "
            f"(default: {DEFAULT_RESULTS_INTERVAL}). Improvements that happen inside one "
            "interval are sent as one event."
        ),
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help=(
            f"Shortest gap between two progress events, in seconds "
            f"(default: {DEFAULT_PROGRESS_INTERVAL})."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        # Where the dumps are is decided once, at startup: a machine with no dataset
        # directory should fail here rather than on somebody's first request.
        data_dir = (
            Path(arguments.data_dir) if arguments.data_dir else default_data_dir()
        )
        app = create_app(
            data_dir=data_dir,
            database=None if arguments.database is None else Path(arguments.database),
            search_workers=arguments.search_workers,
            batch_size=arguments.batch_size,
            step_seconds=arguments.step_seconds,
            results_interval=arguments.results_interval,
            progress_interval=arguments.progress_interval,
            registry=SearchRegistry(),
        )
    except DatasetNotAvailableError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    try:
        listening = bind(arguments.host, arguments.port)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    host, port = listening.getsockname()[0], listening.getsockname()[1]
    print(f"{LISTENING_PREFIX}http://{host}:{port}", flush=True)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=arguments.log_level,
            lifespan="on",
        )
    )
    try:
        server.run(sockets=[listening])
    except KeyboardInterrupt:  # Ctrl-C is how a person stops a server, not a failure
        pass
    finally:
        listening.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
