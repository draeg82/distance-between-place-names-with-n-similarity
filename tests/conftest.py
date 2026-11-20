"""Suite-wide guards: it cannot leave this machine, and it never reads a committed dataset.

Both guards apply to every test except one marked `slow` - and `slow` tests are
deselected by default (`addopts` in `pyproject.toml`), so the suite that runs on every
`uv run pytest` cannot reach the network or a committed dump even by accident. The network
guard allows the loopback interface, because the HTTP tests talk to a real search server on
this machine; anything aimed anywhere else fails the test.
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

#: Path to the committed cities1000 dump, available only to `@pytest.mark.slow` tests.
CITIES_1000 = REPO_ROOT / "cities1000.txt"


@pytest.fixture(scope="module")
def real_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`cities1000.txt` imported once, for slow end-to-end and timing tests."""
    from support import import_dump

    path = tmp_path_factory.mktemp("real-places") / "places.sqlite3"
    connection = import_dump(path, CITIES_1000)
    connection.close()
    return path


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
    """A socket that may only talk to this machine.

    The HTTP tests need real sockets - a search server on the loopback interface, a real
    client that can hang up mid-stream - and nothing else in the suite needs any. So the
    guard is about leaving the machine rather than about touching a socket: connecting
    anywhere but loopback still fails the test.
    """

    def connect(self, address: object) -> None:
        _check_loopback(address)
        super().connect(address)

    def connect_ex(self, address: object) -> int:
        _check_loopback(address)
        return super().connect_ex(address)


#: The addresses a default-suite test may talk to. Everything else is the internet.
LOOPBACK_ADDRESSES = frozenset(("127.0.0.1", "::1", "localhost"))


def _check_loopback(address: object) -> None:
    host = address[0] if isinstance(address, tuple) and address else address
    if isinstance(host, bytes):
        host = host.decode()
    if isinstance(host, str) and host in LOOPBACK_ADDRESSES:
        return
    raise AssertionError(
        f"the default suite must not need the network: refused a connection to {address!r}. "
        "Only the loopback interface is available - the suite runs offline."
    )


def _guarded_connect(real_connect: Any) -> Any:
    """`socket.create_connection` with the same loopback rule as `socket.connect`."""

    def guarded(address: object, *arguments: Any, **keyword_arguments: Any) -> socket.socket:
        _check_loopback(address)
        return real_connect(address, *arguments, **keyword_arguments)

    return guarded


def _blocked_getaddrinfo(real_getaddrinfo: Any) -> Any:
    def guarded(host: object, port: object, *arguments: Any, **keyword_arguments: Any) -> Any:
        if str(host) not in LOOPBACK_ADDRESSES:
            raise AssertionError(
                f"the default suite must not need the network: refused to resolve {host!r}"
            )
        return real_getaddrinfo(host, port, *arguments, **keyword_arguments)

    return guarded


@pytest.fixture(autouse=True)
def offline_and_dataset_free(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a default-suite test reaches off this machine or reads a committed dump."""
    if request.node.get_closest_marker("slow") is not None:
        return
    monkeypatch.setattr(builtins, "open", _blocked_open_factory(builtins.open))
    # `pathlib.Path.open` looks `io.open` up on the module at call time, and
    # `builtins.open is io.open`, so both names have to be replaced.
    monkeypatch.setattr(io, "open", _blocked_open_factory(io.open))
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    monkeypatch.setattr(socket, "create_connection", _guarded_connect(socket.create_connection))
    # Name resolution is how a test reaches something that is not this machine, so a name
    # has to be on the loopback list before it is looked up at all.
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo(socket.getaddrinfo))
