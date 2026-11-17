"""The events this API streams, and the shape of each one.

One module owns the wire format so the contract is stated once. A search is four kinds of
event, newline-delimited JSON, one per line:

`started`
    What is about to be searched, how much of it, and what it is expected to cost. Sent
    before any pair exists, so a client can show a cost and a denominator.
`progress`
    How far the scan has got. Repeated on an interval, whether or not anything was found -
    a search that has found nothing yet is not a hung one.
`results`
    The current top-K. Repeated only when the ranking actually improved, and no more often
    than the coalescing interval, so a hot scan cannot flood a client.
`done`
    Final counts and the elapsed total, or `error` instead if the search failed.

The legacy CSV column order is an export format and lives in `distnsimilarity.export`; this
is the shape an interface reads.
"""

from __future__ import annotations

import json
from typing import Any

from distnsimilarity.core.places import Place
from distnsimilarity.core.topk import RankedPair

#: Newline-delimited JSON: `Content-Type` for a streamed search.
NDJSON_MEDIA_TYPE = "application/x-ndjson"

#: How often a `results` event may be sent while a scan keeps improving. One event per
#: improvement would be one per pair on a good stretch of a big dump; this is the interval a
#: client is asked to render at instead.
DEFAULT_RESULTS_INTERVAL = 0.25


def place_payload(place: Place) -> dict[str, Any]:
    """A place as the API reports it. `display_name` is for showing; matching used
    `asciiname` and nothing else."""
    return {
        "geonameid": place.geonameid,
        "asciiname": place.asciiname,
        "display_name": place.display_name,
        "latitude": place.latitude,
        "longitude": place.longitude,
        "population": place.population,
        "country_code": place.country_code,
    }


def ranked_payload(ranked: RankedPair) -> dict[str, Any]:
    """One row of the top-K: both places, the distance, and how alike their names are.

    `character_difference` is the count a row reports and `differing_positions` the indexes
    an interface highlights; both come from the ranking, which derives them from the two
    names, so no adapter has to agree with anybody about what a pair looks like.
    """
    return {
        "first": place_payload(ranked.first),
        "second": place_payload(ranked.second),
        "distance_km": ranked.distance_km,
        "character_difference": ranked.character_difference,
        "differing_positions": list(ranked.differing_position_indexes),
    }


def ndjson_line(payload: dict[str, Any]) -> str:
    """One record, one line. `ensure_ascii=False` keeps a name like `São Paulo` readable."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def started_line(
    *,
    search_id: str,
    request: dict[str, Any],
    stats: dict[str, Any],
    estimate: dict[str, Any],
) -> str:
    """The event before the first pair: what is about to be searched, and what it will cost.

    `request` is the echo of what the caller asked for, with what it resolved to: `dataset` is
    the population threshold the search filters on (null for every place in the database),
    `dataset_label` the words for it, and `database` the cache the places will be read from - so
    a client about to watch a long scan knows which cache it is reading. `requested_dataset` is
    the spelling the caller sent, which is often just the digits.
    """
    return ndjson_line(
        {
            "type": "started",
            "search_id": search_id,
            **request,
            "places": stats["places"],
            "population_floor": stats["population_floor"],
            "candidate_pairs": stats["candidate_pairs"],
            # Zero of both, deliberately: the search has not found anything or compared
            # anything yet, and a client that draws a progress bar starts it at nothing.
            "improvements": 0,
            "pairs": 0,
            "elapsed_seconds": 0.0,
            "estimate": estimate,
        }
    )


def progress_line(search_id: str, stats: dict[str, Any]) -> str:
    """How far the scan has got, in the units it is measured in.

    `pairs_compared` walks towards `candidate_pairs`, which was counted before the first
    comparison; `elapsed_seconds` is the honest answer to "is this slow or is this stuck"; and
    `estimated_seconds_remaining` is this search's own pace read forward, which says nothing at
    all until the search is far enough in to have a pace.
    """
    elapsed = stats["elapsed_seconds"]
    share = stats["share_compared"]
    remaining = None if not share or share <= 0 else elapsed * (1 / share - 1)
    return ndjson_line(
        {
            "type": "progress",
            "search_id": search_id,
            "places": stats["places"],
            "pairs_compared": stats["pairs_compared"],
            "candidate_pairs": stats["candidate_pairs"],
            "share_compared": share,
            "pairs": stats["pairs"],
            "improvements": stats["improvements"],
            "elapsed_seconds": elapsed,
            "estimated_seconds_remaining": None if remaining is None else round(remaining, 3),
        }
    )


def results_line(
    search_id: str, results: list[RankedPair], *, direction: str, top_k: int, stats: dict[str, Any]
) -> str:
    """The current top-K, whole. A client replaces its list rather than merging into it.

    `improvements` counts every improvement, including the ones this event coalesced away, so a
    client can say "412 better pairs since the last time you saw this list" instead of
    implying that only the four events so far ever changed anything. `seen_pairs` is how many
    pairs the search has found at all, which the list is a window onto and not a count of.
    """
    return ndjson_line(
        {
            "type": "results",
            "search_id": search_id,
            "direction": direction,
            "top_k": top_k,
            "improvements": stats["improvements"],
            "seen_pairs": stats["pairs"],
            "pairs_compared": stats["pairs_compared"],
            "elapsed_seconds": stats["elapsed_seconds"],
            "results": [ranked_payload(ranked) for ranked in results],
        }
    )


def done_line(search_id: str, summary: dict[str, Any]) -> str:
    """The event after the last one. The summary is the search's own statistics, so these
    numbers are the ones the registry reports for the same search."""
    return ndjson_line({"type": "done", "search_id": search_id, **summary})


def error_line(message: str, search_id: str | None = None) -> str:
    """A search that failed mid-stream says so on the stream, with the id it failed under."""
    return ndjson_line({"type": "error", "search_id": search_id, "message": message})
