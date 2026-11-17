"""Portability guards: no personal paths, declared dependencies, an owned environment."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from support import dump_row, place_database, write_dump

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".toml", ".md", ".cfg", ".ini", ".json", ".ts", ".tsx", ".html", ".sh", ".yml", ".yaml"}
SKIP_DIRECTORIES = {".git", ".venv", "node_modules", "dist", "__pycache__", ".pi", "build"}

# The shapes a personal absolute path takes: a Windows user folder, a cloud-storage
# folder, or a Unix home directory baked into source. Written in pieces so that this
# guard file cannot match itself.
SLASH = chr(47)
BACKSLASH = chr(92)


def _home_directory_pattern(segment: str) -> re.Pattern[str]:
    return re.compile(SLASH + segment + SLASH + r"[^\s\"']*")


PERSONAL_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(f"[A-Za-z]:{BACKSLASH}{BACKSLASH}", re.IGNORECASE),
    _home_directory_pattern("Users"),
    _home_directory_pattern("home"),
    re.compile("One" "Drive"),
    re.compile("App" "Data"),
)


def source_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if SKIP_DIRECTORIES & set(path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


class TestNoPersonalPaths:
    def test_no_source_file_names_a_personal_absolute_path(self) -> None:
        offenders: list[str] = []
        for path in source_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in PERSONAL_PATH_PATTERNS:
                for match in pattern.finditer(text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {match.group(0)}")
        assert offenders == []

    def test_no_source_file_resolves_a_path_against_the_home_directory(self) -> None:
        home_lookup_fragments = ("Path" + ".home()", "expanduser" + '("~")', "expanduser" + "('~')")
        offenders: list[str] = []
        for path in source_files():
            if path.suffix != ".py":
                continue
            text = path.read_text(encoding="utf-8")
            if any(fragment in text for fragment in home_lookup_fragments):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []


class TestDeclaredDependencies:
    def pyproject(self) -> dict[str, object]:
        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)

    def test_every_runtime_dependency_carries_a_version_floor(self) -> None:
        dependencies = self.pyproject()["project"]["dependencies"]
        assert dependencies
        unpinned = [
            dependency
            for dependency in dependencies
            if not re.search(r">=|\^|==", dependency)
        ]
        assert unpinned == []

    def test_the_distance_library_and_the_asgi_server_are_declared(self) -> None:
        declared = {
            re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0].strip().lower()
            for dependency in self.pyproject()["project"]["dependencies"]
        }
        # haversine: its call signature is relied on. uvicorn: the search API needs an
        # ASGI server, and it was missing from the machine it was developed on.
        assert {"haversine", "uvicorn", "fastapi"} <= declared

    def test_the_project_declares_a_python_floor(self) -> None:
        assert str(self.pyproject()["project"]["requires-python"]).startswith(">=")

    def test_the_installed_distance_library_satisfies_the_floor(self) -> None:
        from importlib.metadata import version

        assert version("haversine") >= "2.8.0"


class TestOwnedEnvironment:
    def test_the_suite_runs_in_a_virtual_environment_rather_than_a_system_python(self) -> None:
        """Success must not depend on which interpreter wins the PATH search, so the
        suite must not be able to pass on a hand-managed distribution Python."""
        assert Path(sys.prefix) != Path(sys.base_prefix), (
            "not running inside a virtual environment: install with `uv sync --extra dev` "
            "and run the suite as `uv run pytest`"
        )
        active = os.environ.get("VIRTUAL_ENV")
        assert active, "run the suite as `uv run pytest` so the project environment is used"
        assert Path(active).resolve() == Path(sys.prefix).resolve()
        assert "Anaconda" not in sys.version, f"running on a hand-managed interpreter: {sys.version}"

    def test_the_package_is_importable_the_way_the_console_scripts_import_it(
        self, tmp_path: Path
    ) -> None:
        """A broken editable install otherwise shows up as a console script that cannot
        import its own package, with no hint as to why."""
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, "-c", "import distnsimilarity.cli"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{sys.executable} cannot import the package: {result.stderr.strip()}. "
            "Re-run `uv sync --extra dev`; on macOS see README, 'If the tool will not start'."
        )

    def test_a_search_survives_a_shadowed_system_interpreter(self, tmp_path: Path) -> None:
        """Shadowing the developer's Anaconda environment must not stop the tool: the
        entry point runs from `.venv`, not from whatever wins the PATH search."""
        if shutil.which("uv") is None:
            pytest.skip("uv not installed")
        shadow = tmp_path / "shadow-bin"
        shadow.mkdir()
        for name in ("python", "python3", "python3.11", "python3.12", "python3.13"):
            (shadow / name).write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
            os.chmod(shadow / name, 0o755)

        database = place_database(
            tmp_path / "places.sqlite3",
            [dump_row(1, "Analac", 0.0, 0.0), dump_row(2, "Bnalac", 0.0, 1.0)],
        )
        output = tmp_path / "out.csv"
        environment = dict(os.environ, PATH=f"{shadow}{os.pathsep}{os.environ['PATH']}")

        result = subprocess.run(
            ["uv", "run", "--project", str(REPO_ROOT), "distnsimilarity-search",
             "--database", str(database), "--n", "1", "--output", str(output), "--quiet"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "Bnalac" in output.read_text(encoding="utf-8")
