"""Dataset resolution: repo-relative, caller-supplied, and loud when unresolvable."""

from __future__ import annotations

from pathlib import Path

import pytest

from distnsimilarity.datasets import (
    DatasetNotAvailableError,
    available_datasets,
    default_data_dir,
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
