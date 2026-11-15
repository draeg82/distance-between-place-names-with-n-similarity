"""Suite-wide guards: offline, and never reading a committed dataset.

Both guards apply to every test except one marked `slow` - and `slow` tests are
deselected by default (`addopts` in `pyproject.toml`), so the suite that runs on every
`uv run pytest` cannot touch the network or a committed dump even by accident.
"""

from __future__ import annotations

import builtins
import io
import socket
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The three dumps committed to this repository, by path: a test may write its own
#: `cities1000.txt` into a temporary directory, it just may not read the real one.
COMMITTED_DATASETS = frozenset(
    (REPO_ROOT / name).resolve() for name in ("cities1000.txt", "cities5000.txt", "cities15000.txt")
)


def _blocked_open_factory(real_open: Any) -> Any:
    def guarded_open(file: object, *arguments: Any, **keyword_arguments: Any) -> Any:
        path = Path(str(file)).resolve()
        if path in COMMITTED_DATASETS:
            raise AssertionError(
                f"{path.name} is a committed dataset: the default suite must run on fixtures "
                "small enough to check by hand. Mark the test `slow` if it really needs it."
            )
        return real_open(file, *arguments, **keyword_arguments)

    return guarded_open


class _BlockedSocket(socket.socket):
    def __init__(self, *arguments: Any, **keyword_arguments: Any) -> None:
        raise AssertionError(
            "the default suite must not need the network: it runs offline on fixtures"
        )


@pytest.fixture(autouse=True)
def offline_and_dataset_free(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a default-suite test opens a socket or reads a committed dump."""
    if request.node.get_closest_marker("slow") is not None:
        return
    monkeypatch.setattr(builtins, "open", _blocked_open_factory(builtins.open))
    # `pathlib.Path.open` looks `io.open` up on the module at call time, and
    # `builtins.open is io.open`, so both names have to be replaced.
    monkeypatch.setattr(io, "open", _blocked_open_factory(io.open))
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", _BlockedSocket)
