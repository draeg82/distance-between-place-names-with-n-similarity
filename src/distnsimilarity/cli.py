"""The command-line adapter: a headless search over the place database.

This is an adapter, not the seam: everything it does is available by calling
``distnsimilarity.search.open_search``, and that is what both this and the HTTP API use.
What the command line adds is a CSV file, a progress display, and a Ctrl-C handler that
stops the search instead of abandoning it.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Callable, Sequence

from tqdm import tqdm

from distnsimilarity.core.places import PlaceRowError
from distnsimilarity.core.sweep import DEFAULT_DIFFERENCE_LIMIT, DifferenceRange
from distnsimilarity.datasets import DatasetNotAvailableError
from distnsimilarity.db.schema import DEFAULT_DATABASE_NAME, DatabaseSchemaError
from distnsimilarity.export.csv_export import LegacyCsvWriter
from distnsimilarity.search import (
    CacheNotImportedError,
    InvalidSearchRequest,
    SearchControl,
    SearchRequest,
    SearchSession,
    build_metadata,
    open_search,
)

#: "Stopped, and the process was asked to stop": the 128 + SIGINT convention, so a script
#: wrapping a cancelled search can tell it apart from a search that found nothing.
EXIT_CANCELLED = 130


def _make_difference_range(
    limit: int | None, exclude_identical_names: bool
) -> DifferenceRange:
    """Build a difference range from CLI arguments, applying the default limit."""
    return DifferenceRange(
        limit=DEFAULT_DIFFERENCE_LIMIT if limit is None else limit,
        exclude_identical_names=exclude_identical_names,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="distnsimilarity-search",
        description=(
            "Find pairs of places whose ASCII names differ by exactly n characters, and "
            "write the distance between each pair as a CSV."
        ),
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help=(
            "Dataset to search: a population threshold (15000) or the dump name it is named "
            "after (cities15000.txt), meaning 'cities of 15,000 or more people'. Omit it to "
            "search every place in the database."
        ),
    )
    parser.add_argument(
        "--n",
        type=int,
        required=True,
        help="Character-difference value: pairs match at exactly this many differing positions.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the CSV. No default: the caller always says where it goes.",
    )
    parser.add_argument(
        "--min-population",
        type=int,
        default=None,
        help="Population floor: places below it are never compared.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help=(
            "The place database to search, at this path. Default: $DISTNSIMILARITY_DB, or "
            f"{DEFAULT_DATABASE_NAME} beside the dumps, which is where "
            "distnsimilarity-import writes it."
        ),
    )
    parser.add_argument(
        "--full-comparison",
        action="store_true",
        help=(
            "Compare every place against every later place instead of only places whose "
            "ASCII name is the same length. Same pairs and the same rows, much slower: the "
            "fast path reports one name length at a time, so only the order a listing arrives "
            "in differs. Useful for checking the prefilter against itself."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the progress display.")
    # From-a-place mode parameters.
    parser.add_argument(
        "--from-place",
        type=int,
        default=None,
        help=(
            "From-a-place mode: the GeoNames id to search outward from. "
            "When given, --n is replaced by --difference-limit."
        ),
    )
    parser.add_argument(
        "--difference-limit",
        type=int,
        default=None,
        help=(
            "From-a-place mode: sweep from 0 differing characters up to this many. "
            "Default: 3. Ignored unless --from-place is given."
        ),
    )
    parser.add_argument(
        "--exclude-identical-names",
        action="store_true",
        default=False,
        help=(
            "From-a-place mode: leave identical-name pairs out of the results. "
            "Ignored unless --from-place is given."
        ),
    )
    return parser


def _stop_on_signal(control: SearchControl) -> Callable[[int, FrameType | None], None]:
    """A signal handler that stops the search rather than killing the process.

    A search writes its CSV as it goes, so an interrupted run leaves a CSV holding exactly
    the pairs that were found - which is the same partial result a client that stops
    reading gets over HTTP.
    """

    def handle(signum: int, _frame: FrameType | None) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        control.cancel(f"stopped by {name}")

    return handle


def write_pairs(session: SearchSession, output: Path, *, quiet: bool = False) -> int:
    """Write every pair the search reports, as it reports it, and say how many there were."""
    pairs = 0
    metadata = build_metadata(session)
    with LegacyCsvWriter(output, metadata=metadata) as writer:
        for match in tqdm(
            session.pairs, disable=quiet, unit="pair", desc=f"matching n={session.request.n}"
        ):
            writer.write(match.first, match.second, match.distance_km)
            pairs += 1
    return pairs


def main(argv: Sequence[str] | None = None, *, control: SearchControl | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        request = SearchRequest(
            n=arguments.n if arguments.from_place is None else None,
            dataset=arguments.dataset,
            min_population=arguments.min_population,
            equal_length_prefilter=not arguments.full_comparison,
            database=arguments.database,
            from_place=arguments.from_place,
            difference=(
                None
                if arguments.from_place is None
                else _make_difference_range(arguments.difference_limit, arguments.exclude_identical_names)
            ),
        )
    except InvalidSearchRequest as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    search_control = SearchControl() if control is None else control
    try:
        session = open_search(request, control=search_control)
    except (
        DatasetNotAvailableError,
        PlaceRowError,
        CacheNotImportedError,
        DatabaseSchemaError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    install = getattr(signal, "signal", None)
    previous: object = None
    if install is not None:
        try:
            previous = install(signal.SIGINT, _stop_on_signal(search_control))
        except ValueError:  # not the main thread: the caller keeps its own control
            install = None

    try:
        pairs = write_pairs(session, arguments.output, quiet=arguments.quiet)
    finally:
        if install is not None and previous is not None:
            install(signal.SIGINT, previous)  # type: ignore[arg-type]

    stats = session.stats
    if stats.cancelled:
        print(
            f"cancelled after {stats.pairs_emitted} pair(s) at n={request.n}: "
            f"{search_control.reason}. {arguments.output} holds what was found.",
            file=sys.stderr,
        )
        return EXIT_CANCELLED

    # The dataset words are what the person chose; a stricter floor is what the count beside
    # them was taken at, so the line says both rather than letting two places sit under
    # "cities of 5,000 or more people" and look like a filter that misfired.
    floor = ""
    if request.min_population is not None and stats.population_floor != request.dataset_threshold:
        floor = f", at a population floor of {stats.population_floor:,}"

    if session.request.mode == "everywhere":
        print(
            f"{pairs} pair(s) at n={arguments.n} from {session.name}{floor} "
            f"({stats.places} places in {session.database_path}) written to {arguments.output}"
        )
    else:
        limit = arguments.difference_limit
        limit_str = limit if limit is not None else 3
        print(
            f"{pairs} pair(s) from {session.origin.asciiname} ({session.origin.country_code}), "
            f"range 0..{limit_str} at {session.name}{floor} "
            f"({stats.places} places in {session.database_path}) written to {arguments.output}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
