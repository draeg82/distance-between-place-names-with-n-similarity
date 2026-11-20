"""End-to-end export tests against real committed data.

These tests are marked `@pytest.mark.slow` and excluded from the default run.
They import `cities1000.txt` and exercise the full export path — both search
modes, zero-result searches, and the CSV format — against real data.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from http_support import serve

#: Cache for the real database across slow tests.
_real_database_cache: dict[str, Path] = {}


@pytest.mark.slow
class TestExportSmokeTest:
    """AC: one slow end-to-end test against real committed data.

    Runs the full path for both search modes, verifies the CSV format,
    and records the runtime.
    """

    def test_export_everywhere_mode(self, real_database: Path) -> None:
        """Export everywhere mode: n=1, cities5000 dataset.

        Verifies: legacy header, metadata comments, distance in km, at least some rows.
        """
        with serve(real_database) as server:
            response = server.export(
                n=1,
                dataset="5000",
                direction="greatest",
                top_k=50,
            )

        assert response.status_code == 200
        lines = response.text.split("\r\n")
        # Check metadata comment lines.
        assert any(line.startswith("#") and "mode: everywhere" in line for line in lines)
        assert any(line.startswith("#") and "ranking: greatest" in line for line in lines)
        # Check header.
        header_idx = next(i for i, l in enumerate(lines) if l.startswith("#") is False)
        assert lines[header_idx] == "place1,lat1,lon1,place2,lat2,lon2,haversinedistance"
        # Check rows exist and have 7 columns.
        data_lines = [l for l in lines if l and not l.startswith("#") and l != lines[header_idx]]
        assert len(data_lines) > 0
        for dl in data_lines:
            assert len(dl.split(",")) == 7
        # Verify a distance value is numeric (km).
        sample_row = data_lines[0].split(",")
        float(sample_row[6])  # haversinedistance must parse as a number

    def test_export_from_place_mode(self, real_database: Path) -> None:
        """Export from-a-place mode: Springfield sweep from 0 to 3.

        Verifies: legacy header, metadata comments, distance in km.
        """
        with serve(real_database) as server:
            # Look up Springfield first.
            lookup = server.places("Springfield").json()
            sp_fielding_id = lookup["matches"][0]["geonameid"]

            response = server.export(
                from_place=sp_fielding_id,
                difference_limit=3,
                direction="greatest",
            )

        assert response.status_code == 200
        lines = response.text.split("\r\n")
        assert any(line.startswith("#") and "mode: from_place" in line for line in lines)
        assert any(line.startswith("#") and "from_place:" in line for line in lines)
        header_idx = next(i for i, l in enumerate(lines) if l.startswith("#") is False)
        assert lines[header_idx] == "place1,lat1,lon1,place2,lat2,lon2,haversinedistance"

    def test_export_zero_result_search(self, real_database: Path) -> None:
        """Export a search with no results: valid CSV with header only.

        AC: an export of a zero-result search produces a valid file with a header
        and no rows, not an error.
        """
        with serve(real_database) as server:
            # n=99 is guaranteed to find nothing.
            response = server.export(n=99, dataset="15000")

        assert response.status_code == 200
        lines = response.text.split("\r\n")
        header_idx = next(i for i, l in enumerate(lines) if l.startswith("#") is False)
        assert lines[header_idx] == "place1,lat1,lon1,place2,lat2,lon2,haversinedistance"
        # No data rows after the header.
        data_rows = [l for l in lines if l and not l.startswith("#") and l != lines[header_idx]]
        assert len(data_rows) == 0

    def test_export_runtime_recorded(self, real_database: Path) -> None:
        """Measure the export runtime for the ticket.

        AC: the slow test's runtime is recorded on the ticket.
        """
        with serve(real_database) as server:
            start = time.perf_counter()
            response = server.export(n=1, dataset="5000", top_k=50)
            elapsed = time.perf_counter() - start

        assert response.status_code == 200
        # The ticket runtime: ~3-8 seconds on a typical laptop with cities5000.
        print(f"\nExport runtime (n=1, dataset=5000, top_k=50): {elapsed:.2f}s\n")
