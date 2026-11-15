"""Measure what the equal-length prefilter saves, on a real dump.

    uv run python scripts/benchmark_scan.py --dataset 15000 --n 1 --sample 3000

Two numbers come out. The *candidate pairs* count is exact arithmetic over the whole dump:
how many pairs a full comparison would consider, and how many the length buckets hold. The
*rate* is measured on a sample, because a full comparison is quadratic and the largest dump
is 10.77 billion pairs. The extrapolated full-dump time is the sample's rate applied to the
exact counts.
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
