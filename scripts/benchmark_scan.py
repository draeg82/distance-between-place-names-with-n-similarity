"""Measure what the equal-length prefilter saves, on a real dump.

    uv run python scripts/benchmark_scan.py --dataset 15000 --n 1 --sample 3000
    uv run python scripts/benchmark_scan.py --dataset 1000 --n 1 --database

Two numbers come out. The *candidate pairs* count is exact arithmetic over the whole dump:
how many pairs a full comparison would consider, and how many the length buckets hold. The
*rate* is measured on a sample, because a full comparison is quadratic and the largest dump
is 10.77 billion pairs. The extrapolated full-dump time is the sample's rate applied to the
exact counts.

`--database` adds the database-backed walk of the same places: it builds a temporary place
database from the dump, prints the scope and the lookup's query plan, and times the walk
against the in-memory one at the same population floor. The floor keeps that run short - a
whole unfiltered walk of `cities1000.txt` is hundreds of millions of comparisons.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from distnsimilarity.core.places import Place, read_places  # noqa: E402
from distnsimilarity.core.prefilter import EqualLengthGroups, all_pair_count  # noqa: E402
from distnsimilarity.core.scan import scan  # noqa: E402
from distnsimilarity.datasets import resolve_dataset  # noqa: E402


def timed(places: list[Place], threshold: int, *, prefilter: bool) -> tuple[float, int, int]:
    """Seconds taken, pairs reported, comparisons performed."""
    comparisons = (
        EqualLengthGroups.of(places).pair_count()
        if prefilter
        else all_pair_count(len(places))
    )
    started = time.perf_counter()
    reported = sum(1 for _ in scan(places, threshold, equal_length_prefilter=prefilter))
    return time.perf_counter() - started, reported, comparisons


def timed_database(
    dump: Path, threshold: int, character_difference: int, floor: int
) -> None:
    """The same candidate set, walked out of a database instead of a list."""
    import tempfile

    from distnsimilarity.core.population import above_floor
    from distnsimilarity.db import connect, import_places, scan_database, scope_for
    from distnsimilarity.db.schema import query_plan

    with tempfile.TemporaryDirectory(prefix="distnsimilarity-benchmark-") as directory:
        path = Path(directory) / "places.sqlite3"
        connection = connect(path)
        started = time.perf_counter()
        report = import_places(connection, [(threshold, dump)])
        import_seconds = time.perf_counter() - started
        print(
            f"database: {report.summary()} in {import_seconds:.2f}s "
            f"({report.input_rows / import_seconds:,.0f} rows/s)"
        )

        scope = scope_for(connection, floor)
        print(f"scope at floor {floor:,}: {scope.summary()}")
        plan = query_plan(
            connection,
            "SELECT geonameid, asciiname, display_name, latitude, longitude, population, "
            "country_code FROM place WHERE name_length = ? AND population >= ? "
            "ORDER BY population, geonameid LIMIT 2000",
            (8, floor),
        )
        print(f"candidate lookup: {' / '.join(plan.splitlines())}")

        places = [place for place in read_places(dump) if above_floor(place.population, floor)]
        started = time.perf_counter()
        from_database = sum(1 for _ in scan_database(connection, character_difference, floor))
        database_seconds = time.perf_counter() - started

        started = time.perf_counter()
        from_memory = sum(1 for _ in scan(places, character_difference))
        memory_seconds = time.perf_counter() - started

        if from_database != from_memory:
            raise SystemExit(
                f"the database reported {from_database} pairs, the dump reported {from_memory}"
            )
        print(
            f"database walk: {from_database:,} pairs in {database_seconds:.3f}s "
            f"({scope.pair_count / database_seconds:,.0f} comparisons/s)"
        )
        print(
            f"in-memory walk: {from_memory:,} pairs in {memory_seconds:.3f}s "
            f"({scope.pair_count / memory_seconds:,.0f} comparisons/s) - same pairs both ways"
        )
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument(
        "--sample",
        type=int,
        default=3000,
        help="Places to actually walk. A full comparison is quadratic in this number.",
    )
    parser.add_argument(
        "--database",
        action="store_true",
        help="Also time the database-backed walk of the same places, in a temporary database.",
    )
    parser.add_argument(
        "--database-floor",
        type=int,
        default=100_000,
        help="Population floor for the database comparison, which walks everything it is given.",
    )
    arguments = parser.parse_args()

    path = resolve_dataset(arguments.dataset)
    places = read_places(path)
    full_pairs = all_pair_count(len(places))
    bucketed_pairs = EqualLengthGroups.of(places).pair_count()

    print(f"dump: {path.name} ({len(places)} places), n={arguments.n}")
    print(
        f"candidate pairs over the whole dump: {full_pairs:,} full vs {bucketed_pairs:,} "
        f"bucketed - {bucketed_pairs / full_pairs:.1%} of the work, "
        f"{1 - bucketed_pairs / full_pairs:.1%} less"
    )

    sample = places[: min(arguments.sample, len(places))]
    whole_dump = {False: full_pairs, True: bucketed_pairs}
    for label, prefilter in (("full comparison", False), ("equal-length prefilter", True)):
        seconds, reported, comparisons = timed(sample, arguments.n, prefilter=prefilter)
        rate = comparisons / seconds if seconds else float("inf")
        print(
            f"{label}: walked {len(sample)} places in {seconds:.3f}s - {comparisons:,} "
            f"comparisons at {rate:,.0f}/s, {reported:,} pairs reported; whole dump at "
            f"that rate: {whole_dump[prefilter] / rate / 60:.1f} min"
        )

    if arguments.database:
        from distnsimilarity.datasets import dataset_threshold

        print()
        timed_database(path, dataset_threshold(path.name), arguments.n, arguments.database_floor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
