"""The command-line adapter: caller-supplied input, output and character-difference value."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from distnsimilarity import cli
from support import dump_row, write_dump

# One degree of arc on a sphere of the mean Earth radius: 6371.0088 km x pi / 180.
ONE_DEGREE_KM = 111.1950802335329

HEADER = ["place1", "lat1", "lon1", "place2", "lat2", "lon2", "haversinedistance"]


@pytest.fixture()
def dump(tmp_path: Path) -> Path:
    return write_dump(
        tmp_path / "cities42.txt",
        [
            dump_row(1, "Analac", 0.0, 0.0, population=100),
            dump_row(2, "Bnalac", 0.0, 1.0, population=100),
            dump_row(3, "Cnalac", 0.0, 2.0, population=100),
            dump_row(4, "Anala", 0.0, 3.0, population=100),  # unequal length: never compared
            dump_row(5, "Xnalax", 0.0, 4.0, population=100),  # two differences at n=1: no match
        ],
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class TestSearch:
    def test_a_search_writes_every_matching_pair_in_file_order(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        exit_code = cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])

        assert exit_code == 0
        rows = read_rows(output)
        assert [(row["place1"], row["place2"]) for row in rows] == [
            ("Analac", "Bnalac"),
            ("Analac", "Cnalac"),
            ("Bnalac", "Cnalac"),
        ]

    def test_the_header_is_the_legacy_contract(self, dump: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])
        assert output.read_text(encoding="utf-8").splitlines()[0] == ",".join(HEADER)

    def test_coordinates_are_reported_for_both_places(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])
        row = read_rows(output)[0]
        distance_km = float(row.pop("haversinedistance"))
        assert row == {
            "place1": "Analac",
            "lat1": "0.0",
            "lon1": "0.0",
            "place2": "Bnalac",
            "lat2": "0.0",
            "lon2": "1.0",
        }
        assert distance_km == pytest.approx(ONE_DEGREE_KM, abs=0.01)

    def test_distances_are_in_kilometres(self, dump: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])
        rows = read_rows(output)
        assert float(rows[0]["haversinedistance"]) == pytest.approx(ONE_DEGREE_KM, abs=0.01)
        assert float(rows[1]["haversinedistance"]) == pytest.approx(2 * ONE_DEGREE_KM, abs=0.02)

    def test_a_search_that_matches_nothing_still_writes_a_header(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        exit_code = cli.main(["--dataset", str(dump), "--n", "9", "--output", str(output)])
        assert exit_code == 0
        assert output.read_text(encoding="utf-8").splitlines() == [",".join(HEADER)]

    def test_the_character_difference_value_is_matched_exactly(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "2", "--output", str(output)])
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Analac", "Xnalax"),
            ("Bnalac", "Xnalax"),
            ("Cnalac", "Xnalax"),
        ]


class TestCallerSuppliedArguments:
    @pytest.mark.parametrize("missing", ["--dataset", "--n", "--output"])
    def test_every_input_is_supplied_by_the_caller(
        self, dump: Path, tmp_path: Path, missing: str
    ) -> None:
        argv = ["--dataset", str(dump), "--n", "1", "--output", str(tmp_path / "out.csv")]
        argv.pop(argv.index(missing))
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code != 0

    def test_nothing_is_written_before_the_arguments_check_out(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "never-written.csv"
        with pytest.raises(SystemExit):
            cli.main(["--dataset", str(dump), "--output", str(output)])
        assert not output.exists()


class TestFailureModes:
    def test_an_unresolvable_dataset_fails_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "dumps"
        empty.mkdir()
        write_dump(empty / "cities1000.txt", [dump_row(1, "Nowhere", 0.0, 0.0)])
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(empty))

        exit_code = cli.main(["--dataset", "500", "--n", "1", "--output", str(tmp_path / "o.csv")])

        assert exit_code == 1
        error = capsys.readouterr().err
        assert "500" in error
        assert "1000" in error
        assert not (tmp_path / "o.csv").exists()


class TestFileHandling:
    def test_records_are_separated_by_crlf_and_the_file_is_utf_8(
        self, dump: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])
        raw = output.read_bytes()
        assert raw.startswith(b"place1,lat1,lon1,place2,lat2,lon2,haversinedistance\r\n")
        assert raw.count(b"\r\n") == 4
        raw.decode("utf-8")

    def test_the_input_dump_is_read_as_utf_8(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "cities7.txt",
            [
                dump_row(1, "Geneve", 0.0, 0.0, name="Genève"),
                dump_row(2, "Genevx", 0.0, 1.0, name="Genèvx"),
            ],
        )
        output = tmp_path / "out.csv"
        cli.main(["--dataset", str(dump), "--n", "1", "--output", str(output)])
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Geneve", "Genevx")
        ]

    def test_the_tool_runs_from_an_unrelated_working_directory(
        self, dump: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        output = elsewhere / "out.csv"
        exit_code = cli.main(["--dataset", str(dump), "--n", "1", "--output", "out.csv"])
        assert exit_code == 0
        assert (elsewhere / "out.csv").exists()


def test_haversine_default_radius_is_pinned_by_the_expected_km() -> None:
    """Sanity check on the hand-computed figure used above: 1 degree of arc on a
    sphere of the mean Earth radius (6371.0088 km)."""
    assert 6371.0088 * math.pi / 180 == pytest.approx(ONE_DEGREE_KM, abs=1e-9)
