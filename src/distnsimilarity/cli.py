"""The command-line adapter: a headless search over one GeoNames dump.

This is an adapter, not the seam: everything it does is available by calling
``distnsimilarity.core`` directly, and that is what the tests do.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from distnsimilarity.core.places import PlaceRowError, read_places
from distnsimilarity.core.scan import scan
from distnsimilarity.datasets import DatasetNotAvailableError, resolve_dataset
from distnsimilarity.export.csv_export import LegacyCsvWriter


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
        required=True,
        help=(
            "Population threshold (15000), dump file name (cities15000.txt) or path. "
            "No default: the caller always says which data to read."
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
        "--full-comparison",
        action="store_true",
        help=(
            "Compare every place against every later place instead of only places whose "
            "ASCII name is the same length. Same pairs, same order, same CSV; much slower. "
            "Useful for checking the prefilter against itself."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the progress display.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        path = resolve_dataset(arguments.dataset)
        places = read_places(path)
    except (DatasetNotAvailableError, PlaceRowError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    pairs = 0
    with LegacyCsvWriter(arguments.output) as writer:
        matches = scan(
            places,
            arguments.n,
            arguments.min_population,
            equal_length_prefilter=not arguments.full_comparison,
        )
        for match in tqdm(matches, disable=arguments.quiet, unit="pair", desc="matching"):
            writer.write(match.first, match.second, match.distance_km)
            pairs += 1

    print(
        f"{pairs} pair(s) at n={arguments.n} from {path.name} "
        f"({len(places)} places) written to {arguments.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
