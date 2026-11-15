"""The generated place database: one row per GeoNames id, every field a search needs, and a
report of anything the dumps got wrong.

The deduplication is the point of the database. The committed dumps contain each other -
`cities15000.txt` is a subset of `cities5000.txt` which is a subset of `cities1000.txt` - so
choosing between them means choosing between three copies of the same places. One table of
146,782 places replaces 230,160 rows.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from distnsimilarity.db import candidate_places, default_database_path, place_count
from distnsimilarity.db.import_command import main as import_main
from distnsimilarity.db.importing import import_places
from distnsimilarity.db.schema import (
    DEFAULT_DATABASE_NAME,
    INDEX_NAMES,
    PLACE_COLUMNS,
    DatabaseSchemaError,
    connect,
    index_names,
    query_plan,
)
from distnsimilarity.datasets import DatasetNotAvailableError, dataset_threshold
from support import dump_fields, dump_row, import_dump, write_dump, write_dump_lines

REPO_ROOT = Path(__file__).resolve().parent.parent


def nested_dumps(directory: Path) -> list[tuple[int, Path]]:
    """Three dumps that contain each other, the way the committed ones do.

    `cities1000.txt` holds ids 1-4, `cities5000.txt` holds 3-4 and `cities15000.txt` holds
    4 - so 7 input rows hold 4 places, and each place's smallest dump is countable by eye.
    """
    small = write_dump(
        directory / "cities1000.txt",
        [dump_row(index, f"Aba{index}", 10.0 + index, 20.0 + index, population=2_000) for index in (1, 2, 3, 4)],
    )
    middle = write_dump(
        directory / "cities5000.txt",
        [
            dump_row(3, "Aba3", 13.0, 23.0, population=6_000),
            dump_row(4, "Aba4", 14.0, 24.0, population=7_000),
        ],
    )
    large = write_dump(
        directory / "cities15000.txt",
        [dump_row(4, "Aba4", 14.0, 24.0, population=17_000)],
    )
    return [(1_000, small), (5_000, middle), (15_000, large)]


def rows_of(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row[column] for column in PLACE_COLUMNS)
        for row in connection.execute("SELECT * FROM place ORDER BY geonameid")
    ]


class TestOneRowPerGeoNamesId:
    def test_a_dump_becomes_one_row_per_place(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "cities2.txt",
            [
                dump_row(7, "Lima", -12.05, -77.04, name="Lima", population=8_012_109, countrycode="PE"),
                dump_row(8, "Loma", 1.0, 2.0, name="La Loma", population=1_500),
            ],
        )
        connection = import_dump(tmp_path / "places.sqlite3", dump)
        assert place_count(connection) == 2
        stored = connection.execute("SELECT * FROM place WHERE geonameid = 7").fetchone()
        assert stored["asciiname"] == "Lima"
        assert stored["display_name"] == "Lima"
        assert (stored["latitude"], stored["longitude"]) == (-12.05, -77.04)
        assert stored["population"] == 8_012_109
        assert stored["country_code"] == "PE"

    def test_the_table_holds_every_field_a_search_and_a_screen_need(self, tmp_path: Path) -> None:
        connection = import_dump(
            tmp_path / "places.sqlite3",
            write_dump(tmp_path / "cities2.txt", [dump_row(1, "Aba", 1.0, 2.0)]),
        )
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(place)")
        }
        assert columns == set(PLACE_COLUMNS), (
            "the table has to carry the ASCII name, the display name, the coordinates, the "
            "population, the country code, the name length and the source dataset - and "
            f"nothing else; found {sorted(columns)}"
        )

    def test_the_name_length_is_the_length_of_the_ascii_name(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "cities2.txt",
            [dump_row(1, "Aba", 1.0, 2.0, name="Ciudad de Aba muy larga")],
        )
        row = import_dump(tmp_path / "places.sqlite3", dump).execute(
            "SELECT name_length, source_threshold FROM place"
        ).fetchone()
        assert row["name_length"] == 3, "the ASCII name is 3 characters, the display name is 23"
        assert row["source_threshold"] == 2

    def test_nested_dumps_yield_one_row_per_place(self, tmp_path: Path) -> None:
        dumps = nested_dumps(tmp_path)
        connection = connect(tmp_path / "places.sqlite3")
        report = import_places(connection, dumps)
        assert report.input_rows == 7
        assert report.places == 4, "7 rows over three nested dumps hold 4 places"
        assert report.deduplicated == 3
        assert place_count(connection) == 4

    def test_the_source_threshold_is_the_smallest_dump_that_held_the_place(
        self, tmp_path: Path
    ) -> None:
        connection = connect(tmp_path / "places.sqlite3")
        import_places(connection, nested_dumps(tmp_path))
        assert {
            int(row["geonameid"]): int(row["source_threshold"])
            for row in connection.execute("SELECT geonameid, source_threshold FROM place")
        } == {
            1: 1_000,  # only in cities1000.txt
            2: 1_000,
            3: 1_000,  # also in cities5000.txt, so the smaller threshold records it
            4: 1_000,  # in all three
        }

    def test_the_smallest_dump_records_a_place_whatever_order_it_arrives(
        self, tmp_path: Path
    ) -> None:
        dumps = nested_dumps(tmp_path)
        connection = connect(tmp_path / "places.sqlite3")
        import_places(connection, list(reversed(dumps)))
        assert [
            int(row["source_threshold"])
            for row in connection.execute("SELECT source_threshold FROM place ORDER BY geonameid")
        ] == [1_000, 1_000, 1_000, 1_000], (
            "the import walks dumps from the smallest threshold up, so a place is recorded "
            "by the most inclusive dump that contains it even when the caller lists them "
            "the other way round"
        )

    def test_the_values_written_are_the_small_dumps(self, tmp_path: Path) -> None:
        connection = connect(tmp_path / "places.sqlite3")
        import_places(connection, nested_dumps(tmp_path))
        row = connection.execute("SELECT population FROM place WHERE geonameid = 4").fetchone()
        assert row["population"] == 2_000, (
            "a place recorded by cities1000.txt keeps the values from that dump; the copies "
            "of it in cities5000.txt (7000) and cities15000.txt (17000) are ignored, not merged"
        )


class TestIdempotence:
    def test_importing_the_same_dump_twice_adds_nothing(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "cities2.txt",
            [dump_row(1, "Aba", 1.0, 2.0), dump_row(2, "Abe", 3.0, 4.0)],
        )
        database = tmp_path / "places.sqlite3"
        connection = import_dump(database, dump)
        first = rows_of(connection)

        report = import_places(connection, [(2, dump)])

        assert report.inserted == 0, "a second import must not duplicate a GeoNames id"
        assert report.deduplicated == 2
        assert report.places == 2
        assert rows_of(connection) == first, "and must not change a value either"

    def test_importing_nested_dumps_twice_adds_nothing(self, tmp_path: Path) -> None:
        database = tmp_path / "places.sqlite3"
        connection = connect(database)
        dumps = nested_dumps(tmp_path)
        import_places(connection, dumps)
        first = rows_of(connection)

        report = import_places(connection, dumps)

        assert report.input_rows == 7
        assert report.inserted == 0
        assert report.deduplicated == 7
        assert report.places == 4
        assert rows_of(connection) == first

    def test_recreate_rebuilds_from_only_the_dumps_given(self, tmp_path: Path) -> None:
        dumps = nested_dumps(tmp_path)
        database = tmp_path / "places.sqlite3"
        connection = connect(database)
        import_places(connection, dumps)
        assert place_count(connection) == 4

        report = import_places(connection, [dumps[2]], recreate_database=True)

        assert report.places == 1, "cities15000.txt holds one place, so the table holds one"
        row = connection.execute("SELECT * FROM place").fetchone()
        assert row["geonameid"] == 4
        assert row["source_threshold"] == 15_000
        assert row["population"] == 17_000, "rebuilt from the largest dump, so those are its values"


class TestWhatTheReportNames:
    """The summary line is the build record of a generated database, so it has to be true."""

    def test_the_report_names_the_database_sqlite_actually_wrote(self, tmp_path: Path) -> None:
        dump = write_dump(
            tmp_path / "cities1000.txt",
            [dump_row(1, "Lima", 12.0, -77.0, population=9_000)],
        )
        database = tmp_path / "places.sqlite3"
        connection = connect(database)

        report = import_places(connection, [(1_000, dump)])

        assert report.database == str(database), "the file that was opened, not a label"
        assert str(database) in report.summary()
        connection.close()


class TestADatabaseThisVersionCannotRead:
    """A generated file can outlive the version that wrote it."""

    @pytest.fixture
    def from_the_future(self, tmp_path: Path) -> Path:
        database = tmp_path / "places.sqlite3"
        connection = connect(database)
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
        connection.close()
        return database

    def test_a_newer_schema_is_refused_rather_than_misread(self, from_the_future: Path) -> None:
        with pytest.raises(DatabaseSchemaError) as raised:
            connect(from_the_future)

        assert "newer version" in str(raised.value)
        assert "re-import" in str(raised.value), "the message says what to do about it"

    def test_the_import_command_reports_it_instead_of_tracebacking(
        self, from_the_future: Path, tmp_path: Path, capsys
    ) -> None:
        dump = write_dump(
            tmp_path / "cities1000.txt",
            [dump_row(1, "Aba", 1.0, 2.0), dump_row(2, "Abe", 3.0, 4.0)],
        )

        exit_code = import_main(["--database", str(from_the_future), "--dataset", str(dump)])

        assert exit_code == 1
        assert "newer version" in capsys.readouterr().err


class TestUnusableRowsAreReported:
    @pytest.fixture
    def dodgy_dump(self, tmp_path: Path) -> Path:
        rows = [
            dump_row(1, "Aba", 1.0, 2.0),
            dump_row(2, "Ade", 120.0, 2.0),  # latitude off the globe
            dump_row(3, "Afe", 3.0, -200.0),  # longitude off the globe
            dump_row(4, "Aga", "not a number", 4.0),  # not a coordinate at all
        ]
        short_row = dump_fields(geonameid=5, asciiname="Aha", latitude="5.0", longitude="6.0")
        short_row.pop()  # no modificationdate: a row the dumps cut short
        path = write_dump(tmp_path / "cities3.txt", rows)
        lines = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]
        write_dump_lines(
            path,
            [
                *lines,
                short_row,
                dump_fields(geonameid=6, asciiname="Aia", latitude="7.0", longitude="8.0"),
            ],
        )
        return path

    def test_rows_with_unusable_coordinates_are_reported_not_dropped(
        self, tmp_path: Path, dodgy_dump: Path
    ) -> None:
        connection = connect(tmp_path / "places.sqlite3")
        report = import_places(connection, [(3, dodgy_dump)])

        assert len(report.unreadable) == 4, (
            "two coordinates off the globe, one that is not a number and one short row: "
            f"reported {len(report.unreadable)}"
        )
        assert [row.line_number for row in report.unreadable] == [2, 3, 4, 5]
        assert all(row.source == "cities3.txt" for row in report.unreadable)
        refused = {row.line_number: row.reason for row in report.unreadable}
        assert "latitude" in refused[2] and "120" in refused[2]
        assert "longitude" in refused[3] and "-200" in refused[3]
        assert "latitude" in refused[4]
        assert "19" in refused[5], "a short row says how many fields it was missing"

    def test_the_usable_rows_still_make_it_in(self, tmp_path: Path, dodgy_dump: Path) -> None:
        connection = connect(tmp_path / "places.sqlite3")
        import_places(connection, [(3, dodgy_dump)])
        assert [
            int(row["geonameid"]) for row in connection.execute("SELECT geonameid FROM place")
        ] == [1, 6], "the two good rows are imported; the four bad ones are reported instead"

    def test_the_import_command_prints_each_refused_line(
        self, tmp_path: Path, dodgy_dump: Path, capsys
    ) -> None:
        exit_code = import_main(
            ["--database", str(tmp_path / "p.sqlite3"), "--dataset", str(dodgy_dump)]
        )
        captured = capsys.readouterr()

        assert exit_code == 1
        for line_number in (2, 3, 4, 5):
            assert f"line {line_number} of cities3.txt" in captured.err, captured.err
        assert "2 place(s)" in captured.out
        assert "6 row(s) read" in captured.out

    def test_the_import_command_is_quiet_when_every_row_was_usable(
        self, tmp_path: Path, capsys
    ) -> None:
        dump = write_dump(tmp_path / "cities4.txt", [dump_row(1, "Aba", 1.0, 2.0)])
        exit_code = import_main(
            ["--database", str(tmp_path / "p.sqlite3"), "--dataset", str(dump)]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.err == ""
        assert "1 place(s)" in captured.out
        assert "0 row(s) refused" in captured.out


class TestIndexesAndQueryPlans:
    @pytest.fixture
    def database(self, tmp_path: Path) -> sqlite3.Connection:
        dump = write_dump(
            tmp_path / "cities5.txt",
            [
                dump_row(index, f"Name{index}", index / 10, index / 5, population=index * 1_000)
                for index in range(1, 40)
            ],
        )
        return import_dump(tmp_path / "places.sqlite3", dump)

    def test_the_database_carries_the_indexes_both_search_modes_need(self, database) -> None:
        assert set(INDEX_NAMES) <= set(index_names(database)), (
            "everywhere mode needs (name_length, population, geonameid) to walk a bucket, "
            "the population index serves the counts shown before a search, and from-a-place "
            f"mode needs the name index to offer choices; found {index_names(database)}"
        )

    def test_walking_a_name_length_group_is_an_index_lookup_not_a_table_scan(
        self, database
    ) -> None:
        plan = query_plan(
            database,
            "SELECT geonameid FROM place WHERE name_length = ? AND population >= ? "
            "ORDER BY population, geonameid LIMIT 10",
            (6, 1_000),
        )
        assert plan.startswith("SEARCH"), plan
        assert "idx_place_name_length_population" in plan, plan

    def test_a_floor_above_every_place_still_uses_the_index_and_finds_nothing(
        self, database
    ) -> None:
        """Setting the selector too high is a normal thing to do to a search box, so the
        empty answer has to come from the same index lookup as a full one."""
        plan = query_plan(
            database,
            "SELECT geonameid FROM place WHERE name_length = ? AND population >= ? "
            "ORDER BY population, geonameid LIMIT 10",
            (6, 2_000_000_000),
        )
        assert plan.startswith("SEARCH"), plan
        assert "idx_place_name_length_population" in plan, plan
        assert list(candidate_places(database, 2_000_000_000)) == []

    def test_the_from_a_place_lookup_is_an_index_lookup_too(self, database) -> None:
        plan = query_plan(
            database,
            "SELECT geonameid FROM place WHERE name_length = ? AND population >= ? "
            "AND geonameid <> ? ORDER BY population, geonameid",
            (6, 1_000, 12),
        )
        assert plan.startswith("SEARCH"), plan
        assert "idx_place_name_length_population" in plan, plan

    def test_finding_a_place_by_name_is_an_index_lookup(self, database) -> None:
        plan = query_plan(database, "SELECT geonameid FROM place WHERE asciiname = ?", ("Aba",))
        assert plan.startswith("SEARCH"), plan
        assert "idx_place_asciiname" in plan, plan

    def test_counting_the_groups_behind_an_estimate_never_touches_the_table(
        self, database
    ) -> None:
        plan = query_plan(
            database,
            "SELECT name_length, COUNT(*) FROM place WHERE population >= ? "
            "GROUP BY name_length ORDER BY name_length",
            (5_000,),
        )
        assert "USING COVERING INDEX idx_place_name_length_population" in plan, plan
        assert "TEMP B-TREE" not in plan, (
            "the groups come out of the index already in name-length order, so the cost "
            f"estimate sorts nothing; plan was {plan}"
        )

    def test_counting_a_floor_seeks_the_population_index(self, database) -> None:
        plan = query_plan(database, "SELECT COUNT(*) FROM place WHERE population >= ?", (5_000,))
        assert plan.startswith("SEARCH"), plan
        assert "idx_place_population" in plan, plan

    def test_only_a_query_no_index_can_answer_walks_the_table(self, database) -> None:
        plan = query_plan(database, "SELECT asciiname, latitude FROM place", ())
        assert plan.startswith("SCAN place"), plan
        assert "INDEX" not in plan, "a full walk is what reading something unindexed costs"
        plan = query_plan(database, "SELECT COUNT(*) FROM place", ())
        assert "COVERING INDEX" in plan, "counting places is indexed, so it is not a full walk"


class TestTheImportCommandPicksItsDumps:
    def test_the_default_is_every_dump_in_the_data_directory(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        data_dir = tmp_path / "dumps"
        nested_dumps(data_dir)
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(data_dir))

        exit_code = import_main([])

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        assert (data_dir / DEFAULT_DATABASE_NAME).is_file(), default_database_path(data_dir)
        assert "4 place(s)" in captured.out
        assert "7 row(s) read, 3 duplicate row(s) ignored" in captured.out
        assert "0 row(s) refused" in captured.out

    def test_the_default_database_path_is_beside_the_dumps_not_in_a_home_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(tmp_path / "somewhere else"))
        assert default_database_path() == tmp_path / "somewhere else" / "places.sqlite3"

    def test_a_named_subset_of_dumps_is_all_that_is_imported(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        data_dir = tmp_path / "dumps"
        nested_dumps(data_dir)
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(data_dir))

        exit_code = import_main(["--dataset", "15000"])

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        database = connect(data_dir / DEFAULT_DATABASE_NAME)
        assert place_count(database) == 1
        assert "cities15000.txt" in captured.out

    def test_the_same_dump_named_two_ways_is_imported_once(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        data_dir = tmp_path / "dumps"
        nested_dumps(data_dir)
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(data_dir))

        exit_code = import_main(["--dataset", "15000", "--dataset", "cities15000.txt"])

        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        assert "1 place(s) in" in captured.out
        assert "1 row(s) read, 0 duplicate row(s) ignored" in captured.out
        assert "cities15000.txt" in captured.out, "named twice, listed once"
        assert "2 row(s) read, 1 duplicate row(s) ignored" not in capsys.readouterr().out

    def test_an_unresolvable_dataset_fails_loudly_and_writes_nothing(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        data_dir = tmp_path / "dumps"
        nested_dumps(data_dir)
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(data_dir))
        target = tmp_path / "nothing.sqlite3"

        exit_code = import_main(["--database", str(target), "--dataset", "500"])

        assert exit_code == 1
        assert "500" in capsys.readouterr().err
        assert not target.exists(), "a failed import must not leave a half-built database"
        assert not (tmp_path / "nothing.sqlite3-wal").exists()

    def test_a_dataset_with_no_number_in_its_name_says_what_it_needed(
        self, tmp_path: Path, capsys
    ) -> None:
        dump = write_dump(tmp_path / "places-of-mine.txt", [dump_row(1, "Aba", 1.0, 2.0)])
        exit_code = import_main(["--database", str(tmp_path / "p.sqlite3"), "--dataset", str(dump)])
        assert exit_code == 1
        assert "population threshold" in capsys.readouterr().err
        with pytest.raises(DatasetNotAvailableError):
            dataset_threshold("places-of-mine.txt")

    def test_no_dumps_in_the_data_directory_names_the_flag_that_fixes_it(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(tmp_path / "empty"))
        target = tmp_path / "places.sqlite3"

        exit_code = import_main(["--database", str(target)])

        assert exit_code == 1
        error = capsys.readouterr().err
        assert "--dataset" in error and "DISTNSIMILARITY_DATA_DIR" in error
        assert not target.exists(), "nothing to import means nothing written"

    def test_the_generated_database_is_not_something_the_repository_tracks(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "*.sqlite3" in gitignore, (
            "the database is generated by distnsimilarity-import and must never be "
            "committed, so it has to be ignored by name"
        )
        check = subprocess.run(
            ["git", "check-ignore", "-q", DEFAULT_DATABASE_NAME],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if check.returncode == 128:
            pytest.skip("git is not available to confirm the generated database is ignored")
        assert check.returncode == 0, (
            f"{DEFAULT_DATABASE_NAME} is a build artefact: git check-ignore should ignore it, "
            f"and said rc={check.returncode} {check.stderr.strip()}"
        )


class TestThresholdsAndFloorsAreTheSameNumber:
    def test_a_dump_threshold_and_a_population_floor_are_one_thing(self) -> None:
        assert dataset_threshold(5_000) == 5_000
        assert dataset_threshold("5000") == 5_000
        assert dataset_threshold("cities5000") == 5_000
        assert dataset_threshold("cities5000.txt") == 5_000
        assert dataset_threshold("/somewhere/cities15000.txt") == 15_000
        assert dataset_threshold("1000") == dataset_threshold("cities1000.txt") == 1_000

    def test_a_number_that_is_not_a_threshold_is_refused(self) -> None:
        for request in ("", "   ", "cities", "cities-five-thousand", "Cités5000", None, 1.5):
            with pytest.raises((DatasetNotAvailableError, TypeError)):
                dataset_threshold(request)  # type: ignore[arg-type]
