"""Dataset resolution: repo-relative, caller-supplied, and loud when unresolvable.

The second half is the search's half: a dataset is a population rule somebody chooses from a
dropdown, so its words are part of the interface, and the spellings that are not a choice an
importer needs (`""`, a file path) have to be refused rather than guessed at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from distnsimilarity.datasets import (
    WHOLE_DATABASE_LABEL,
    DatasetNotAvailableError,
    available_datasets,
    dataset_label,
    default_data_dir,
    parse_dataset_threshold,
    resolve_dataset,
)


def write_dump(directory: Path, threshold: int) -> Path:
    path = directory / f"cities{threshold}.txt"
    path.write_text("1\tName\tName\t\t0.0\t0.0\tP\tPPL\tXX\t\t\t\t\t\t1\t\t\tUTC\t2024-01-01\n", encoding="utf-8")
    return path


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "dumps"
    directory.mkdir()
    write_dump(directory, 1000)
    write_dump(directory, 5000)
    write_dump(directory, 15000)
    return directory


class TestResolveDataset:
    def test_threshold_names_resolve_inside_the_data_dir(self, data_dir: Path) -> None:
        assert resolve_dataset("5000", data_dir=data_dir) == data_dir / "cities5000.txt"

    def test_file_names_resolve_inside_the_data_dir(self, data_dir: Path) -> None:
        assert resolve_dataset("cities15000.txt", data_dir=data_dir) == data_dir / "cities15000.txt"

    def test_an_explicit_path_is_used_as_given(self, tmp_path: Path, data_dir: Path) -> None:
        elsewhere = tmp_path / "somewhere_else.txt"
        elsewhere.write_text("x", encoding="utf-8")
        assert resolve_dataset(str(elsewhere), data_dir=data_dir) == elsewhere.resolve()

    def test_resolution_does_not_depend_on_the_working_directory(
        self, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unrelated = tmp_path / "unrelated-cwd"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)
        assert resolve_dataset("1000", data_dir=data_dir) == data_dir / "cities1000.txt"

    def test_an_unresolvable_request_names_what_was_asked_and_what_exists(
        self, data_dir: Path
    ) -> None:
        with pytest.raises(DatasetNotAvailableError) as excinfo:
            resolve_dataset("500", data_dir=data_dir)
        message = str(excinfo.value)
        assert "500" in message
        assert "cities500.txt" in message
        for threshold in ("1000", "5000", "15000"):
            assert threshold in message


class TestHistoricalDefaultDatasetFailure:
    """Regression: the old script's default pop_limit was 500, and cities500.txt was
    never committed, so the default invocation could only ever fail."""

    def test_the_historical_default_threshold_is_not_available_and_says_so(
        self, data_dir: Path
    ) -> None:
        assert (data_dir / "cities500.txt").exists() is False
        with pytest.raises(DatasetNotAvailableError) as excinfo:
            resolve_dataset("500", data_dir=data_dir)
        assert "cities500.txt" in str(excinfo.value)


class TestAvailableDatasets:
    def test_lists_the_thresholds_present_in_the_data_dir(self, data_dir: Path) -> None:
        assert {d.threshold for d in available_datasets(data_dir)} == {"1000", "5000", "15000"}

    def test_an_empty_data_dir_yields_nothing_rather_than_crashing(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert available_datasets(empty) == []


class TestDefaultDataDir:
    def test_the_default_data_dir_is_inside_the_repository_and_holds_the_dumps(self) -> None:
        directory = default_data_dir()
        assert (directory / "cities1000.txt").is_file()
        assert (directory / "cities5000.txt").is_file()
        assert (directory / "cities15000.txt").is_file()

    def test_a_home_directory_full_of_dumps_is_never_consulted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_dir: Path
    ) -> None:
        """The old tool resolved its input against one person's cloud-storage folder.
        A decoy home directory proves nothing is looked up there."""
        fake_home = tmp_path / "fake-home"
        (fake_home / "Documents").mkdir(parents=True)
        write_dump(fake_home / "Documents", 1000)
        write_dump(fake_home, 5000)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        monkeypatch.delenv("DISTNSIMILARITY_DATA_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        directory = default_data_dir()

        assert directory != fake_home
        assert fake_home not in directory.parents
        assert directory != data_dir  # discovered from the repository, not the fixture

    def test_the_environment_variable_overrides_the_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISTNSIMILARITY_DATA_DIR", str(tmp_path))
        assert default_data_dir() == tmp_path


class TestADatasetIsAPopulationRule:
    """`parse_dataset_threshold` is what a search reads a dataset request as, and
    `dataset_label` is what it is said back - so the two have to agree on one number."""

    def test_every_spelling_of_a_dataset_names_the_same_threshold(self) -> None:
        for request in ("5000", " 5000 ", "cities5000", "cities5000.txt"):
            assert parse_dataset_threshold(request) == 5_000
        assert parse_dataset_threshold("15000") == 15_000

    def test_the_words_carry_the_threshold_with_its_thousands_separated(self) -> None:
        assert dataset_label(5_000) == "cities of 5,000 or more people"
        assert dataset_label(15_000) == "cities of 15,000 or more people"
        assert dataset_label(1_000_000) == "cities of 1,000,000 or more people"

    @pytest.mark.parametrize("threshold", [1_000, 5_000, 15_000, 99_952])
    def test_a_label_is_words_about_people_and_never_a_number_or_a_file(self, threshold: int) -> None:
        label = dataset_label(threshold)
        assert f"{threshold:,}" in label, f"the label has lost the rule it names: {label}"
        assert label != str(threshold), "a bare number is not something to choose from"
        assert f"cities{threshold:,}" not in label.replace(",", ""), (
            f"a dataset is never labelled by the file it is published as: {label}"
        )
        assert ".txt" not in label

    def test_the_words_and_the_filter_are_the_same_number(self) -> None:
        """A label that drifts from the threshold it names is a lie in a dropdown."""
        for threshold in (1_000, 5_000, 15_000):
            words = dataset_label(threshold)
            digits = "".join(character for character in words if character.isdigit())
            assert int(digits) == threshold, f"{words!r} says {digits}, the rule says {threshold}"

    def test_the_choice_that_filters_nothing_is_words_without_a_number(self) -> None:
        assert WHOLE_DATABASE_LABEL == "every place in the database"
        assert not any(character.isdigit() for character in WHOLE_DATABASE_LABEL), (
            "a count in this label moves every time anybody imports, and then two screens "
            f"disagree about one database: {WHOLE_DATABASE_LABEL!r}"
        )

    def test_an_empty_dataset_is_the_whole_database_and_says_so(self) -> None:
        for request in ("", "   "):
            with pytest.raises(DatasetNotAvailableError) as excinfo:
                parse_dataset_threshold(request)
            message = str(excinfo.value)
            assert "dataset" in message
            assert WHOLE_DATABASE_LABEL in message, (
                f"the refusal has to name the choice they meant: {message}"
            )

    def test_a_file_path_is_refused_because_a_search_reads_no_files(self) -> None:
        for request in ("dumps/cities5000.txt", "/tmp/cities5000.txt", r"dumps\cities5000.txt"):
            with pytest.raises(DatasetNotAvailableError) as excinfo:
                parse_dataset_threshold(request)
            message = str(excinfo.value)
            assert "file" in message, f"a path has to be refused as a path: {message}"
            assert "dataset" in message

    @pytest.mark.parametrize("spelling", ["nowhere", "cities", "citieslarge.txt", "5000people"])
    def test_a_request_with_no_threshold_in_it_is_refused_in_dataset_words(
        self, spelling: str
    ) -> None:
        with pytest.raises(DatasetNotAvailableError) as excinfo:
            parse_dataset_threshold(spelling)
        message = str(excinfo.value)
        assert spelling in message, f"the refusal has to quote what was sent: {message}"
        assert "dataset" in message, f"a person choosing a dataset is spoken to as such: {message}"
        assert "5000" in message, f"the refusal has to say what a dataset looks like: {message}"
