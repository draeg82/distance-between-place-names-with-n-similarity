"""Watching a search from a terminal: `distnsimilarity-watch --dataset 15000 --n 1`.

A client of the HTTP API and nothing else. It starts a search, prints what it will cost, and
then prints the top-K again every time the server sends an improved list - which is the same
question the browser asks of the same stream, answered with text so that a person with a
terminal, and a test, can check the streaming works without either.

It ranks nothing, sorts nothing and filters nothing: the order it prints is the order the
`results` events arrived in, because the ranking is the core's and this is not the place for a
second opinion.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Any, Iterator, Sequence, TextIO

import httpx

from distnsimilarity.api.payload import NDJSON_MEDIA_TYPE
from distnsimilarity.api.server import DEFAULT_HOST, DEFAULT_PORT
from distnsimilarity.core.topk import DEFAULT_TOP_K

#: Rows of the top-K to print. The list a search keeps and the list a terminal shows are
#: different things: fifty rows is right for a browser and a wall of text in a shell.
DEFAULT_WATCH_ROWS = 10


def parse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Decoded events, ignoring blank lines. A line that is not JSON is the bug's fault."""
    for line in lines:
        line = line.strip()
        if line:
            yield json.loads(line)


def _place(event: dict[str, Any], which: str) -> str:
    place = event[which]
    return f"{place['asciiname']} ({place['country_code']}, pop {place['population']:,})"


def describe_started(event: dict[str, Any]) -> str:
    """What is about to be searched, and what it is expected to cost."""
    estimate = event["estimate"]
    cost = (
        "no measurement on this server yet, so no time estimate"
        if estimate["seconds"] is None
        else f"about {estimate['seconds']:.0f}s at {estimate['comparisons_per_second']:,.0f} "
        f"comparisons/s ({estimate['basis']})"
    )
    return "\n".join(
        [
            f"search {event['search_id']}: {event['dataset']} at n={event['n']}, "
            f"{event['places']:,} places at population >= {event['population_floor']:,}",
            f"  {event['candidate_pairs']:,} pairs to compare - {cost}",
            f"  keeping the {event['top_k']} {event['direction']} distances",
        ]
    )


def describe_progress(event: dict[str, Any]) -> str:
    share = event["share_compared"]
    percent = "unknown share" if share is None else f"{share * 100:5.1f}%"
    remaining = event["estimated_seconds_remaining"]
    left = "" if remaining is None else f", ~{remaining:.0f}s left"
    return (
        f"  {percent} of {event['candidate_pairs']:,} pairs, "
        f"{event['pairs']} pair(s), {event['elapsed_seconds']:.1f}s elapsed{left}"
    )


def describe_results(event: dict[str, Any], rows: int = DEFAULT_WATCH_ROWS) -> str:
    """The current top-K, in the order the server ranked it."""
    shown = event["results"][:rows]
    lines = [f"  best {event['direction']} distances ({len(event['results'])} kept):"]
    if not shown:
        lines.append("    nothing yet")
    for rank, ranked in enumerate(shown, start=1):
        lines.append(
            f"   {rank:2d}. {ranked['distance_km']:10,.1f} km  "
            f"{_place(ranked, 'first')} <-> {_place(ranked, 'second')}  "
            f"[n={ranked['character_difference']} at {_positions(ranked)}]"
        )
    if len(event["results"]) > rows:
        lines.append(f"    and {len(event['results']) - rows} more")
    return "\n".join(lines)


def _positions(ranked: dict[str, Any]) -> str:
    indexes = ranked["differing_positions"]
    return "nothing" if not indexes else "positions " + ", ".join(str(i) for i in indexes)


def describe_done(event: dict[str, Any]) -> str:
    how = "completed"
    if event["cancelled"]:
        how = f"cancelled ({event['cancel_reason']})"
    elif event["error"]:
        how = f"failed ({event['error']})"
    return (
        f"search {event['search_id']} {how}: {event['pairs']} pair(s) from "
        f"{event['pairs_compared']:,} comparisons in {event['elapsed_seconds']:.1f}s"
    )


def watch(
    events: Iterator[dict[str, Any]],
    *,
    out: TextIO,
    rows: int = DEFAULT_WATCH_ROWS,
    progress: bool = True,
) -> list[dict[str, Any]]:
    """Print an event stream as it arrives; hand back every top-K that was printed.

    Nothing here waits for the end of the stream to print anything, which is the whole point:
    the list appears, then appears again with a better first row.
    """
    printed: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        if kind == "started":
            print(describe_started(event), file=out, flush=True)
        elif kind == "results":
            print(describe_results(event, rows), file=out, flush=True)
            printed.append(event)
        elif kind == "progress" and progress:
            print(describe_progress(event), file=out, flush=True)
        elif kind == "done":
            print(describe_done(event), file=out, flush=True)
        elif kind == "error":
            print(f"search failed: {event['message']}", file=out, flush=True)
    return printed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distnsimilarity-watch",
        description=textwrap.dedent(
            """
            Watch a search's best finds improve over HTTP, in a terminal.

            The search runs on the server; this client only reads what it streams. Press
            Ctrl-C to stop watching, which closes the connection and stops the search.
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}",
        help="Server to watch. Default: %(default)s.",
    )
    parser.add_argument("--dataset", required=True, help="Threshold, dump name or path.")
    parser.add_argument("--n", type=int, required=True, help="Differing character positions.")
    parser.add_argument("--min-population", type=int, default=None, help="Population floor.")
    parser.add_argument(
        "--source", choices=("dump", "database"), default="dump", help="Where the places come from."
    )
    parser.add_argument(
        "--full-comparison",
        action="store_true",
        help="Compare every place against every other, not only equal-length names.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"How many of the best the search keeps (default: {DEFAULT_TOP_K}).",
    )
    parser.add_argument(
        "--direction",
        default="greatest",
        choices=("greatest", "least"),
        help="Which end of the distance range is best.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=DEFAULT_WATCH_ROWS,
        help=f"Rows of the top-K to print (default: {DEFAULT_WATCH_ROWS}).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Print the improving lists only, without the progress lines between them.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    stream = out if out is not None else sys.stdout
    # A parameter nobody set is not sent at all. Sending it empty is a different answer: the
    # server reads `min_population=` as a broken integer and refuses the search.
    parameters = {
        name: value
        for name, value in {
            "dataset": arguments.dataset,
            "n": arguments.n,
            "min_population": arguments.min_population,
            "full_comparison": arguments.full_comparison,
            "source": arguments.source,
            "top_k": arguments.top_k,
            "direction": arguments.direction,
        }.items()
        if value is not None
    }
    url = arguments.url.rstrip("/") + "/v1/search"
    try:
        with httpx.Client(timeout=httpx.Timeout(None, connect=10.0)) as client:
            with client.stream("GET", url, params=parameters) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", "replace")
                    print(f"{url} refused the search: HTTP {response.status_code}", file=stream)
                    for line in body.strip().splitlines():
                        print(f"  {line}", file=stream)
                    return 1
                if response.headers.get("content-type", "").split(";")[0] != NDJSON_MEDIA_TYPE:
                    print(f"{url} did not answer with {NDJSON_MEDIA_TYPE}", file=stream)
                    return 1
                events = parse_events(response.iter_lines())
                printed = watch(
                    events, out=stream, rows=arguments.rows, progress=not arguments.no_progress
                )
    except httpx.HTTPError as error:
        print(f"cannot reach {url}: {error}", file=stream)
        return 1
    except KeyboardInterrupt:
        # Hanging up is how a watcher stops; the server notices and stops the scan.
        print("stopped watching; the server stops the search when the connection closes", file=stream)
        return 130
    if not printed:
        print("the search streamed no top-K at all", file=stream)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
