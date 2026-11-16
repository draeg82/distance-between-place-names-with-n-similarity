"""How the suite runs: one command, offline, and touching nothing it was not told to.

These are guards over the suite itself. The two behavioural ones are armed for every test
in `tests/conftest.py` - a socket, or a committed dump, raises the moment it is touched -
and this module checks that the guards are live and that a run leaves the repository as it
found it.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from distnsimilarity import cli
from distnsimilarity.core.places import read_places
from distnsimilarity.core.scan import scan
from support import dump_row, write_dump

REPO_ROOT = Path(__file__).resolve().parent.parent
PRUNED = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}


def repository_paths() -> set[str]:
    """Every path in the working tree, excluding caches and environments."""
    found: set[str] = set()
    for directory, directories, files in os.walk(REPO_ROOT):
        directories[:] = [name for name in directories if name not in PRUNED]
        for name in files:
            found.add(str(Path(directory, name).relative_to(REPO_ROOT)))
    return found


class TestOneDocumentedCommand:
    def test_the_readme_documents_the_command_that_runs_the_whole_suite(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "uv run pytest" in readme, "the suite must be one command that is written down"

    def test_that_command_also_covers_the_slow_tests_by_leaving_them_out(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert '"-m \'not slow\' --strict-markers"' in pyproject, (
            "the default selection must exclude the slow tests, and an unregistered marker "
            "must be an error rather than a silently ignored typo"
        )
        assert 'markers = [\n    "slow' in pyproject, "the slow marker must be registered"

class TestTheSlowEscapeHatch:
    @pytest.mark.slow
    def test_a_slow_test_may_read_a_committed_dump(self) -> None:
        """Invisible to a normal `uv run pytest`, run by `uv run pytest -m slow`. It
        exists to prove the guard in `conftest.py` has exactly one way round it, and that
        the reader still handles the real dumps."""
        places = read_places(REPO_ROOT / "cities15000.txt")
        assert len(places) > 20_000, f"{len(places)} places read from cities15000.txt"
        assert places[0].geonameid == 3040051
        assert places[0].asciiname == "les Escaldes"
        assert places[0].population >= 15_000, "the dump leads with its largest place"

    @pytest.mark.slow
    def test_a_dump_is_not_filtered_by_the_population_its_name_names(self) -> None:
        """The fact later tickets have to respect: a `cities15000.txt` row can have any
        population at all, so a population floor is something the tool applies."""
        places = read_places(REPO_ROOT / "cities15000.txt")
        below = [place for place in places if place.population < 15_000]
        assert len(below) == 52, (
            f"{len(below)} places in cities15000.txt sit below the 15 000 the file is named "
            "for (the committed dump has 52): the number in a dump's name is not a filter "
            "this tool can lean on"
        )
        assert min(place.population for place in places) == 0


class TestOffline:
    def test_a_default_test_cannot_connect_somewhere_else(self) -> None:
        """The guard is about leaving the machine, not about touching a socket: the HTTP
        tests run a real server on the loopback interface, and nothing else is allowed."""
        with pytest.raises(AssertionError) as blocked:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("93.184.216.34", 80))
        assert "network" in str(blocked.value)
        with pytest.raises(AssertionError) as blocked:
            socket.getaddrinfo("example.com", 443)
        assert "example.com" in str(blocked.value), "the refusal names what was asked for"

    def test_a_loopback_socket_still_works(self) -> None:
        """A round trip to a listening socket on this machine is allowed, so the HTTP tests
        can be written against a real server and a real client."""
        listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        address = listening.getsockname()
        try:
            client = socket.create_connection(address, timeout=5)
            client.sendall(b"ping")
            assert listening.accept()[0].recv(4) == b"ping"
        finally:
            client.close()
            listening.close()

    def test_a_scan_over_two_hundred_places_needs_no_network(self, tmp_path: Path) -> None:
        rows = [
            dump_row(index, f"Place{index:03d}", index / 100, index / 50) for index in range(1, 201)
        ]
        places = read_places(write_dump(tmp_path / "cities2.txt", rows))
        assert sum(1 for _ in scan(places, 1)) > 0, "the fixture should produce pairs at all"

    def test_the_guard_blocks_a_committed_dump_whichever_way_it_is_opened(self) -> None:
        committed = REPO_ROOT / "cities1000.txt"
        assert committed.is_file(), "the committed dumps are part of the repository"
        with pytest.raises(AssertionError) as blocked:
            open(committed, encoding="utf-8")
        assert "cities1000.txt" in str(blocked.value)
        with pytest.raises(AssertionError) as blocked:
            committed.open(encoding="utf-8")
        assert "cities1000.txt" in str(blocked.value)

    def test_a_fixture_dump_with_an_ordinary_name_is_still_readable(self, tmp_path: Path) -> None:
        path = write_dump(tmp_path / "cities7.txt", [dump_row(1, "Aa", 0.0, 0.0)])
        assert len(read_places(path)) == 1


class TestNothingOutsideATemporaryDirectory:
    def test_a_run_writes_only_the_file_it_was_told_to_write(self, tmp_path: Path, capsys) -> None:
        rows = [
            dump_row(index, f"Place{index:03d}", index / 100, index / 50) for index in range(1, 51)
        ]
        dump = write_dump(tmp_path / "cities8.txt", rows)
        output = tmp_path / "results.csv"

        before = repository_paths()
        exit_code = cli.main(
            ["--dataset", str(dump), "--n", "1", "--output", str(output), "--quiet"]
        )
        after = repository_paths()

        assert exit_code == 0, capsys.readouterr().err
        assert output.is_file()
        assert before == after, (
            f"a run changed the working tree: {sorted(after - before)} appeared, "
            f"{sorted(before - after)} disappeared"
        )
