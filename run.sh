#!/usr/bin/env bash
# One command from a fresh clone to the app open in a browser:  ./run.sh
# Options: PORT=9000 ./run.sh      NO_BROWSER=1 ./run.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://docs.astral.sh/uv/). Install it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}"

echo "==> Installing dependencies"
uv sync

if [ ! -f places.sqlite3 ]; then
  echo "==> Building the place database from the GeoNames dumps (about a second)"
  uv run distnsimilarity-import
fi

if [ -z "${NO_BROWSER:-}" ]; then
  # Open the page once the server is up, without blocking the server.
  (
    for _ in $(seq 1 50); do
      if curl -fs "${URL}/v1/datasets" >/dev/null 2>&1; then
        if command -v open >/dev/null 2>&1; then open "$URL"
        elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
        fi
        exit 0
      fi
      sleep 0.2
    done
  ) &
fi

echo "==> Serving on ${URL}  (Ctrl-C to stop)"
exec uv run distnsimilarity-serve --data-dir . --host "$HOST" --port "$PORT"
