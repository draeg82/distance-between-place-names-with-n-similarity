# DistNSimilarity

Find pairs of places whose ASCII names differ by exactly *n* characters, and how far
apart they are. Input is a [GeoNames](https://download.geonames.org/export/dump/) places
dump; the committed dumps are `cities1000.txt`, `cities5000.txt` and `cities15000.txt`
(the number is the minimum population in the dump, not a row count).

## Install

One step, using [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

That creates `.venv` in this directory with its own interpreter and installs everything
in `pyproject.toml`. Nothing here depends on which Python wins the `PATH` search: every
command below runs through `uv run`, which uses `.venv`.

## Run a search from the terminal

```bash
uv run distnsimilarity-search --dataset 15000 --n 1 --output /tmp/out.csv
```

| Flag | Meaning |
| --- | --- |
| `--dataset` | Population threshold (`15000`), file name (`cities15000.txt`) or a path to a dump. Required. |
| `--n` | Character-difference value: pairs match when their equal-length ASCII names differ in exactly this many positions. Required. |
| `--output` | Where to write the CSV. Required. |
| `--min-population` | Optional population floor; places below it are never compared. |

`--dataset`, `--n` and `--output` are always supplied by the caller: the tool has no
default input file and no default output location. A dataset request that cannot be
resolved fails with the request and the list of available datasets.

Output is UTF-8 with `\r\n` record separators, header
`place1,lat1,lon1,place2,lat2,lon2,haversinedistance`, distances in kilometres.

Two searches over the same dumps produce the same rows in the same order.

## Run the tests

```bash
uv run pytest
```

That is the whole default suite: no network access, no writes outside a temporary
directory, and none of the committed dumps loaded, so it finishes in seconds. Tests
that do read real data are marked `slow` and excluded; run them with
`uv run pytest -m slow`.

## Troubleshooting

### If the tool will not start

`ModuleNotFoundError: No module named 'distnsimilarity'` from a console script means the
editable install is not registered. Registration happens through a `.pth` file in the
virtual environment's `site-packages`, and CPython's `site` module **silently skips a
`.pth` file carrying the macOS `hidden` file flag**:

```bash
find .venv -name "*.pth" -flags +hidden   # any output is the problem
```

Confirmed: that flag is what breaks the import — clearing it (`chflags nohidden <file>`)
fixes it, and the same `uv sync` produces no flagged files outside `~/Desktop` and
`~/Documents`. Suspected cause: the cloud-sync agent for those two macOS folders sets the
flag on files installers write there; flagged files have been seen to reappear while
nothing was installing. Which process does it is not established.

Either remedy is environment-only; neither changes this repository:

```bash
# put the environment outside the synced folders, and export the same variable for `uv run`
export UV_PROJECT_ENVIRONMENT="$HOME/venvs/distnsimilarity"
uv sync --extra dev
```

...or keep the clone somewhere other than `~/Desktop` / `~/Documents`.

## Datasets

The dumps are read from the repository root. `DISTNSIMILARITY_DATA_DIR` overrides that
location; nothing is ever resolved against a home directory.

## Portability

Declared runtime dependencies carry version floors (`pyproject.toml`), including
`haversine` — whose two-position tuple call signature is relied on — and `uvicorn`, the
ASGI server behind the search API. Encoding and line endings are explicit on every file
the tool writes. `tests/test_portability.py` keeps this honest: it fails if a personal
absolute path reappears in the source, and it runs the tool from an unrelated working
directory.
