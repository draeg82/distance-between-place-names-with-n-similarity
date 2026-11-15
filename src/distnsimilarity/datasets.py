"""Locating GeoNames dumps.

Datasets live in the repository, never in a home directory or a cloud-storage folder.
The directory is discovered by walking up from this file (and, failing that, from the
current working directory), so the tool runs from any working directory on any machine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR_ENVIRONMENT_VARIABLE = "DISTNSIMILARITY_DATA_DIR"
DUMP_PATTERN = "cities*.txt"
_THRESHOLD_RE = re.compile(r"^cities(?P<threshold>\d+)\.txt$")


class DatasetNotAvailableError(ValueError):
    """Raised when a requested dataset cannot be resolved, naming what was asked for
    and what is actually available."""


@dataclass(frozen=True)
class Dataset:
    """A committed GeoNames dump, named by its population threshold."""

    threshold: str
    path: Path


def _has_dumps(directory: Path) -> bool:
    return any(directory.glob(DUMP_PATTERN))


def _candidate_anchors() -> tuple[Path, ...]:
    """Directories to walk up from, in order of trustworthiness."""
    anchors = [Path(__file__).resolve()]
    cwd = Path.cwd().resolve()
    if cwd not in anchors:
        anchors.append(cwd)
    return tuple(anchors)


def default_data_dir() -> Path:
    """The directory holding the GeoNames dumps.

    Override with ``DISTNSIMILARITY_DATA_DIR``; otherwise the nearest ancestor of this
    file (or of the working directory) containing ``cities*.txt``.
    """
    override = os.environ.get(DATA_DIR_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser().resolve()

    for anchor in _candidate_anchors():
        start = anchor if anchor.is_dir() else anchor.parent
        for directory in (start, *start.parents):
            if _has_dumps(directory):
                return directory
    raise DatasetNotAvailableError(
        f"No dataset directory found: set {DATA_DIR_ENVIRONMENT_VARIABLE} to a directory "
        f"containing {DUMP_PATTERN}, or run inside the repository."
    )


def available_datasets(data_dir: Path | None = None) -> list[Dataset]:
    """Every dump present in ``data_dir``, ordered by population threshold."""
    directory = default_data_dir() if data_dir is None else data_dir
    datasets: list[Dataset] = []
    for path in directory.glob(DUMP_PATTERN):
        match = _THRESHOLD_RE.match(path.name)
        if match is None:
            continue
        datasets.append(Dataset(threshold=match.group("threshold"), path=path))
    return sorted(datasets, key=lambda dataset: int(dataset.threshold))


def dataset_threshold(request: str | int) -> int:
    """The population threshold a dataset request names: ``5000``, ``"5000"``,
    ``"cities5000"`` or ``"cities5000.txt"`` all mean 5000.

    This reads the request, not the disk: a population floor and a dump threshold are the
    same number wearing different jobs, and the interface says one while the search uses the
    other. Anything unparseable fails, naming the accepted forms.
    """
    if isinstance(request, bool):
        raise DatasetNotAvailableError(
            f"a population threshold is a number, not a boolean: {request!r}"
        )
    if isinstance(request, int):
        if request < 0:
            raise DatasetNotAvailableError(f"a population threshold cannot be negative: {request}")
        return request

    text = str(request).strip()
    if text.isdigit():
        return int(text)

    name = Path(text).name
    stem = name[: -len(".txt")] if name.endswith(".txt") else name
    if stem.startswith("cities") and stem[len("cities") :].isdigit():
        return int(stem[len("cities") :])

    raise DatasetNotAvailableError(
        f"no population threshold in {request!r}: give a number (5000) or a dump name "
        "(cities5000, cities5000.txt)"
    )


def resolve_dataset(request: str, data_dir: Path | None = None) -> Path:
    """Turn a caller-supplied dataset request into a file path.

    Accepts a population threshold (``"5000"``), a bare file name
    (``"cities5000.txt"``) or an explicit path. A request without a directory component
    is looked up in the data directory, never in the working directory. Anything that
    cannot be opened fails loudly, naming both the request and what is available.
    """
    directory = default_data_dir() if data_dir is None else data_dir
    requested = Path(request).expanduser()

    if requested.is_absolute() or len(requested.parts) > 1:
        if requested.is_file():
            return requested.resolve()
        candidate_names = {str(requested)}
    else:
        candidate_names = {request, request.strip(), f"cities{request.strip()}.txt"}
        for name in candidate_names:
            candidate = directory / name
            if candidate.is_file():
                return candidate.resolve()

    available = available_datasets(directory)
    listed = ", ".join(f"{d.threshold} ({d.path.name})" for d in available) or "none"
    raise DatasetNotAvailableError(
        f"No dataset for {request!r}: none of "
        f"{', '.join(sorted(repr(name) for name in candidate_names))} exists in {directory}. "
        f"Available datasets: {listed}."
    )
