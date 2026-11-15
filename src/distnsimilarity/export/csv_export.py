"""The legacy CSV result shape, kept as an export format."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Protocol, Sequence, TextIO


from distnsimilarity.core.places import Place


class RowWriter(Protocol):
    """The part of `csv.writer` this module uses, typed without reaching into `_csv`."""

    def writerow(self, row: Sequence[str]) -> object: ...

    def writerows(self, rows: Iterable[Sequence[str]]) -> object: ...

LEGACY_HEADER: tuple[str, ...] = (
    "place1",
    "lat1",
    "lon1",
    "place2",
    "lat2",
    "lon2",
    "haversinedistance",
)
#: Records are separated by CRLF regardless of platform, so a result file is byte-for-byte
#: the same wherever it was produced.
LEGACY_LINE_TERMINATOR = "\r\n"


def legacy_row(first: Place, second: Place, distance_km: float) -> list[str]:
    """One result row: both names, both coordinates, the distance in kilometres."""
    return [
        first.asciiname,
        str(first.latitude),
        str(first.longitude),
        second.asciiname,
        str(second.latitude),
        str(second.longitude),
        str(distance_km),
    ]


def open_legacy_writer(handle: TextIO) -> RowWriter:
    """A CSV writer pinned to the legacy shape, on an already-opened handle."""
    return csv.writer(handle, delimiter=",", lineterminator=LEGACY_LINE_TERMINATOR)


class LegacyCsvWriter:
    """Writes results in the legacy column order, and nothing else.

    Diagnostics never go through this: it takes rows, and only rows.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None
        self._writer: RowWriter | None = None

    def __enter__(self) -> "LegacyCsvWriter":
        self._handle = self._path.open("w", encoding="utf-8", newline="")
        self._writer = open_legacy_writer(self._handle)
        self._writer.writerow(LEGACY_HEADER)
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._handle is not None
        self._handle.close()
        self._handle = None
        self._writer = None

    def write(self, first: Place, second: Place, distance_km: float) -> None:
        assert self._writer is not None, "writer is not open"
        self._writer.writerow(legacy_row(first, second, distance_km))
