"""The command-line adapter: what it reads, what it writes, and what it refuses.

The command line is a thin adapter over `distnsimilarity.search.open_search`, so the pairing
and ranking rules are pinned in `tests/test_search_seam.py`. What is pinned here is the part
only this adapter has: the arguments, the legacy CSV, the exit codes, and where the place
database comes from when the caller does not say.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from distnsimilarity import cli
from distnsimilarity.db.schema import DATABASE_PATH_ENVIRONMENT_VARIABLE
from support import dump_row, place_database, write_dump

# One degree of arc on a sphere of the mean Earth radius: 6371.0088 km x pi / 180.
ONE_DEGREE_KM = 111.1950802335329

HEADER = ["place1", "lat1", "lon1", "place2", "lat2", "lon2", "haversinedistance"]


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    """Five places, three pairs at `n=1`, in the order a database reports them.

    Every place has the same population, so the candidate order - population, then GeoNames id -
    is id order here, which is the file order the assertions below read as.
    """
    return place_database(
        tmp_path / "places.sqlite3",
        [
            dump_row(1, "Analac", 0.0, 0.0, population=100),
            dump_row(2, "Bnalac", 0.0, 1.0, population=100),
            dump_row(3, "Cnalac", 0.0, 2.0, population=100),
            dump_row(4, "Anala", 0.0, 3.0, population=100),  # unequal length: never compared
            dump_row(5, "Xnalax", 0.0, 4.0, population=100),  # two differences at n=1: no match
        ],
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV data rows, skipping comment lines (# ...) and the header."""
    with path.open(encoding="utf-8", newline="") as handle:
        lines = [
            line for line in handle
            if not line.startswith("#")
        ]
    if not lines:
        return []
    # First non-comment line is the header; the rest are data rows.
    reader = csv.DictReader(lines)
    return list(reader)


class TestSearch:
    def test_a_search_writes_every_matching_pair_in_candidate_order(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        exit_code = cli.main(
            ["--database", str(database), "--n", "1", "--output", str(output)]
        )

        assert exit_code == 0
        rows = read_rows(output)
        assert [(row["place1"], row["place2"]) for row in rows] == [
            ("Analac", "Bnalac"),
            ("Analac", "Cnalac"),
            ("Bnalac", "Cnalac"),
        ]

    def test_the_dataset_is_a_population_floor_of_the_cache(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--dataset 5000` means cities of 5,000 or more people, out of the same cache."""
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Analac", 0.0, 0.0, population=9_000),
                dump_row(2, "Bnalac", 0.0, 1.0, population=9_000),
                dump_row(3, "Cnalac", 0.0, 2.0, population=100),
            ],
        )
        output = tmp_path / "out.csv"

        exit_code = cli.main(
            [
                "--database",
                str(database),
                "--dataset",
                "5000",
                "--n",
                "1",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 0
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Analac", "Bnalac")
        ], "the 100-person place was searched anyway"
        assert "cities of 5,000 or more people" in capsys.readouterr().out, (
            "the run says what it searched for, in the words the dataset choice uses"
        )

    def test_a_floor_stricter_than_the_dataset_is_said_on_the_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--dataset 5000 --min-population 20000` searched the 20,000s, not the 5,000s.

        The dataset words are what the person chose, so they stay on the line - but the count
        beside them is the stricter set, and the line has to name that floor. Otherwise two
        places under "cities of 5,000 or more people" reads like a filter that misfired.
        """
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Analac", 0.0, 0.0, population=90_000),
                dump_row(2, "Bnalac", 0.0, 1.0, population=80_000),
                dump_row(3, "Cnalac", 0.0, 2.0, population=9_000),
            ],
        )
        output = tmp_path / "out.csv"

        exit_code = cli.main(
            [
                "--database",
                str(database),
                "--dataset",
                "5000",
                "--min-population",
                "20000",
                "--n",
                "1",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 0, capsys.readouterr().err
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Bnalac", "Analac")
        ], "the 9,000-person place cleared the dataset but not the floor"
        printed = capsys.readouterr().out
        assert "cities of 5,000 or more people" in printed
        assert "population floor of 20,000" in printed, (
            f"the line says what it searched, including which floor the count was taken at: {printed}"
        )

    def test_the_header_is_the_legacy_contract(self, database: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "1", "--output", str(output)])
        # The header may be preceded by comment lines (metadata), so find it.
        text = output.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        assert lines[0] == ",".join(HEADER)

    def test_coordinates_are_reported_for_both_places(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "1", "--output", str(output)])
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

    def test_distances_are_in_kilometres(self, database: Path, tmp_path: Path) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "1", "--output", str(output)])
        rows = read_rows(output)
        assert float(rows[0]["haversinedistance"]) == pytest.approx(ONE_DEGREE_KM, abs=0.01)
        assert float(rows[1]["haversinedistance"]) == pytest.approx(2 * ONE_DEGREE_KM, abs=0.02)

    def test_a_search_that_matches_nothing_still_writes_a_header(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        exit_code = cli.main(
            ["--database", str(database), "--n", "9", "--output", str(output)]
        )
        assert exit_code == 0
        # With comment lines, the file has comments + header; no data rows.
        text = output.read_text(encoding="utf-8")
        non_comment = [line for line in text.splitlines() if not line.startswith("#")]
        assert non_comment == [",".join(HEADER)]

    def test_the_character_difference_value_is_matched_exactly(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "2", "--output", str(output)])
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Analac", "Xnalax"),
            ("Bnalac", "Xnalax"),
            ("Cnalac", "Xnalax"),
        ]


class TestCallerSuppliedArguments:
    @pytest.mark.parametrize("missing", ["--n", "--output"])
    def test_every_input_that_cannot_be_guessed_is_supplied_by_the_caller(
        self, database: Path, tmp_path: Path, missing: str
    ) -> None:
        """What the search compares and where the answer goes are never guessed.

        The dataset and the cache are the two that can be: a dataset defaults to every place in
        the cache, and the cache has a default location like any other tool's data file.
        """
        argv = ["--database", str(database), "--n", "1", "--output", str(tmp_path / "out.csv")]
        argv.pop(argv.index(missing))
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code != 0

    def test_nothing_is_written_before_the_arguments_check_out(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "never-written.csv"
        with pytest.raises(SystemExit):
            cli.main(["--database", str(database), "--output", str(output)])
        assert not output.exists()


class TestThePlaceDatabaseIsTheSource:
    """The cache is not an option any more, so where it comes from has to be pinned."""

    def test_the_default_cache_is_the_one_the_environment_names(
        self, database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """No `--database`, no dump: `DISTNSIMILARITY_DB` says where the places are."""
        monkeypatch.setenv(DATABASE_PATH_ENVIRONMENT_VARIABLE, str(database))
        output = tmp_path / "out.csv"

        exit_code = cli.main(["--n", "1", "--output", str(output)])

        assert exit_code == 0, capsys.readouterr().err
        assert len(read_rows(output)) == 3

    def test_a_database_that_is_not_there_fails_rather_than_reading_a_dump(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dumps = tmp_path / "dumps"
        write_dump(dumps / "cities1000.txt", [dump_row(1, "Analac", 0.0, 0.0)])
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(dumps))
        monkeypatch.delenv(DATABASE_PATH_ENVIRONMENT_VARIABLE, raising=False)
        output = tmp_path / "out.csv"

        exit_code = cli.main(["--n", "1", "--output", str(output)])

        assert exit_code == 1
        error = capsys.readouterr().err
        assert "places.sqlite3" in error, "the refusal names the cache it looked for"
        assert "distnsimilarity-import" in error, "and the command that fills it"
        assert not output.exists()

    def test_a_database_with_nothing_imported_fails_the_same_way(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = place_database(tmp_path / "places.sqlite3", [])
        output = tmp_path / "out.csv"

        exit_code = cli.main(["--database", str(empty), "--n", "1", "--output", str(output)])

        assert exit_code == 1
        assert "only search source" in capsys.readouterr().err

    def test_a_dataset_that_names_no_threshold_is_refused_without_writing(
        self, database: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dataset is a population rule: a path is the import command's business, not this."""
        output = tmp_path / "out.csv"

        exit_code = cli.main(
            ["--database", str(database), "--dataset", str(database), "--n", "1", "--output", str(output)]
        )

        assert exit_code == 2
        assert "dataset" in capsys.readouterr().err
        assert not output.exists()


class TestFileHandling:
    def test_records_are_separated_by_crlf_and_the_file_is_utf_8(
        self, database: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "1", "--output", str(output)])
        raw = output.read_bytes()
        # Header and data lines use CRLF; comments precede the header.
        assert b"place1,lat1,lon1,place2,lat2,lon2,haversinedistance\r\n" in raw
        assert raw.count(b"\r\n") >= 4  # comments + header + data rows
        raw.decode("utf-8")

    def test_a_place_named_in_another_alphabet_comes_through_intact(self, tmp_path: Path) -> None:
        """The display name is what the CSV shows, and it survives the import and the export."""
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Geneve", 0.0, 0.0, name="Genève"),
                dump_row(2, "Genevx", 0.0, 1.0, name="Genèvx"),
            ],
        )
        output = tmp_path / "out.csv"
        cli.main(["--database", str(database), "--n", "1", "--output", str(output)])
        assert [(row["place1"], row["place2"]) for row in read_rows(output)] == [
            ("Geneve", "Genevx")
        ]
        assert b"Gen" in output.read_bytes()

    def test_the_tool_runs_from_an_unrelated_working_directory(
        self, database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        exit_code = cli.main(
            ["--database", str(database), "--n", "1", "--output", "out.csv"]
        )
        assert exit_code == 0
        assert (elsewhere / "out.csv").exists()


def test_haversine_default_radius_is_pinned_by_the_expected_km() -> None:
    """Sanity check on the hand-computed figure used above: 1 degree of arc on a
    sphere of the mean Earth radius (6371.0088 km)."""
    assert 6371.0088 * math.pi / 180 == pytest.approx(ONE_DEGREE_KM, abs=1e-9)
