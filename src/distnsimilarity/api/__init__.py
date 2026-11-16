"""The HTTP adapter: a FastAPI app over the search seam.

Nothing here searches. The app decides what a request means, hands it to
`distnsimilarity.search`, and writes the events out as the scan runs; the search pool, the
registry and this app are the only moving parts.
"""

from __future__ import annotations

from distnsimilarity.api.app import cache_status, create_app, error_response
from distnsimilarity.api.payload import (
    NDJSON_MEDIA_TYPE,
    done_line,
    error_line,
    place_payload,
    progress_line,
    ranked_payload,
    results_line,
    started_line,
)
from distnsimilarity.api.registry import SearchRecord, SearchRegistry, new_search_id
from distnsimilarity.api.runner import (
    DEFAULT_SEARCH_WORKERS,
    SearchPool,
    SearchRunner,
)

__all__ = [
    "DEFAULT_SEARCH_WORKERS",
    "NDJSON_MEDIA_TYPE",
    "SearchPool",
    "SearchRecord",
    "SearchRegistry",
    "SearchRunner",
    "cache_status",
    "create_app",
    "done_line",
    "error_line",
    "error_response",
    "new_search_id",
    "place_payload",
    "progress_line",
    "ranked_payload",
    "results_line",
    "started_line",
]
