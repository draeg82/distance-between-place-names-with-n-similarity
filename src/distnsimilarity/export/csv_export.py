"""The legacy CSV result shape, kept as an export format.

A result is a CSV whose header is the legacy column order (place1, lat1, lon1,
place2, lat2, lon2, haversinedistance). A search's parameters are recorded as
comment lines at the top of the file so a spreadsheet consumer can tell which
export it is looking at.

Diagnostics, progress and errors never land inside the file: the writer only
receives data rows and never touches stderr or the console.
"""

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

#: Comment prefix: the CSV parser must ignore these lines.
COMMENT_PREFIX = "#"


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


def comment_line(text: str) -> str:
    """A comment line the CSV parser will skip."""
    return f"{COMMENT_PREFIX} {text}"


class LegacyCsvWriter:
    """Writes results in the legacy column order, and nothing else.

    Diagnostics never go through this: it takes rows, and only rows. Search
    parameters are written as comment lines before the header, so they never
    interfere with the data.
    """

    def __init__(
        self,
        path: Path,
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._path = path
        self._handle: TextIO | None = None
        self._writer: RowWriter | None = None
        self._metadata = metadata or {}

    def __enter__(self) -> "LegacyCsvWriter":
        self._handle = self._path.open("w", encoding="utf-8", newline="")
        self._writer = open_legacy_writer(self._handle)
        # Write metadata as comment lines before the header.
        for key, value in self._metadata.items():
            self._writer.writerow([comment_line(f"{key}: {value}")])
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
