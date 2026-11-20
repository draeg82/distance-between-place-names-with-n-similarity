#!/usr/bin/env python3
"""Build the world outline the browser draws its map on.

The page needs land: arcs and markers over a blank rectangle tell a reader nothing about
whether two places sit either side of an ocean or in the same valley. Nothing may be fetched
from another machine at run time (`tests/test_browser_app.py` refuses a page that loads a URL)
and no mapping or geospatial dependency may be added on the Python side (#8), so the coastline
is committed as data and the browser is handed four numbers per vertex.

The source is Natural Earth 110m land - public domain, and coarse by design: at the scale of a
world map a coastline is there to say "continent", and 1:110,000,000 is already coarser than the
screen. What this script does is make it smaller and checkable rather than richer:
Douglas-Peucker to a tolerance, one decimal place of a degree (about 11 km, and a world-wide
window is under a thousand pixels wide), and a floor under how small a piece of land survives,
because a speck of an island is unreadable at that scale and every dropped vertex is a vertex
nobody has to review.

The result is a plain JSON asset with its source and tolerance written into it, so the file can
be read as an explanation of itself. It is committed; nothing builds it during install, and
nothing at run time reads this script.

    uv run python scripts/make_world_outline.py \\
        --source https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson \\
        --out src/distnsimilarity/api/static/world-outline.json

`--source` also takes a local file, which is what the committed asset was built from.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request
from pathlib import Path
from typing import Any

NATURAL_EARTH_110M_LAND = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson"
)

#: Where the committed asset says it came from, in words a reader can act on.
SOURCE = "Natural Earth 110m land (public domain), https://www.naturalearthdata.com/"

#: Default tolerance in degrees of a great circle: a vertex is dropped when the land would look
#: the same without it, at the distance it moved.
DEFAULT_TOLERANCE = 0.25

#: A ring whose bounding box is smaller than this in both directions is a smudge at world scale.
DEFAULT_MIN_SPAN = 1.2

#: One decimal of a degree, held to as output.
PLACES = 1


def read_bytes(location: str) -> bytes:
    if "://" in location:
        with urllib.request.urlopen(location, timeout=120) as response:  # noqa: S310 - fixed https source
            return response.read()
    return Path(location).expanduser().read_bytes()


def rings_of(document: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Every outer ring of a GeoJSON land collection, as (longitude, latitude) pairs.

    Only outer rings: land holes (lakes inside land) are a couple of features at 110m and the
    page fills land rather than cutting it out, so a hole would be a ring of nothing.
    """
    rings: list[list[tuple[float, float]]] = []
    for feature in document.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "Polygon":
            polygons = [geometry["coordinates"]]
        elif geometry.get("type") == "MultiPolygon":
            polygons = geometry["coordinates"]
        else:
            continue
        for polygon in polygons:
            if polygon:
                rings.append([(float(point[0]), float(point[1])) for point in polygon[0]])
    return rings


def perpendicular_distance(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    """How far a vertex sits from the straight line between two others, in degrees.

    Longitude is scaled by the cosine of its latitude so that a degree of longitude is worth what
    it is worth on the ground; without that, a tolerance near a pole forgives ten times the error
    it forgives at the equator.
    """
    lon, lat = point
    lon0, lat0 = start
    lon1, lat1 = end
    scale = math.cos(math.radians((lat0 + lat1) / 2))
    ax, ay = lon0 * scale, lat0
    bx, by = lon1 * scale, lat1
    px, py = lon * scale, lat
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(px - ax, py - ay)
    return abs((px - ax) * dy - (py - ay) * dx) / length


def simplify(ring: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas-Peucker over a closed ring, iterative so a long ring cannot blow the stack."""
    if len(ring) < 3:
        return list(ring)
    # A ring repeats its last vertex; simplify the open list and close it again.
    closed = ring[-1] == ring[0]
    points = ring[:-1] if closed else ring
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        anchor, end = points[first], points[last]
        worst, index = 0.0, -1
        for position in range(first + 1, last):
            distance = perpendicular_distance(points[position], anchor, end)
            if distance > worst:
                worst, index = distance, position
        if worst > tolerance and index > 0:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    kept = [point for point, mark in zip(points, keep) if mark]
    if closed and kept:
        kept.append(kept[0])
    return kept


def span(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """How wide and how tall a ring is, in degrees."""
    longitudes = [point[0] for point in ring]
    latitudes = [point[1] for point in ring]
    return max(longitudes) - min(longitudes), max(latitudes) - min(latitudes)


def outline(
    rings: list[list[tuple[float, float]]], tolerance: float, min_span: float
) -> tuple[list[list[list[float]]], dict[str, int]]:
    """Simplify, round, and refuse land too small to see, reporting what each step cost."""
    kept: list[list[list[float]]] = []
    counts = {"in": len(rings), "simplified_out": 0, "too_small": 0, "vertices_in": 0, "vertices_out": 0}
    for ring in rings:
        counts["vertices_in"] += len(ring)
        reduced = simplify(ring, tolerance)
        if len(reduced) < 4:
            counts["simplified_out"] += 1
            continue
        wide, tall = span(reduced)
        if wide < min_span and tall < min_span:
            counts["too_small"] += 1
            continue
        rounded = [[round(lon, PLACES), round(lat, PLACES)] for lon, lat in reduced]
        # Rounding can put two neighbours on the same number, which draws nothing.
        tight = [point for position, point in enumerate(rounded) if position == 0 or point != rounded[position - 1]]
        if len(tight) < 4:
            counts["simplified_out"] += 1
            continue
        if tight[0] != tight[-1]:
            tight.append(list(tight[0]))
        counts["vertices_out"] += len(tight)
        kept.append(tight)
    return kept, counts


def document(
    rings: list[list[list[float]]], tolerance: float, min_span: float, source: str
) -> dict[str, Any]:
    """The asset: coordinates and the facts needed to question them."""
    return {
        "type": "LandOutlines",
        "source": source,
        "tolerance_degrees": tolerance,
        "min_span_degrees": min_span,
        "decimal_places": PLACES,
        "note": (
            "Outer rings only, longitude and latitude in degrees, each ring closed by repeating "
            "its first vertex. Land lies inside a ring; the map is plate carree, so the browser "
            "maps a pair to (lon + 180) / 360 and (90 - lat) / 180 and fills the ring. A ring "
            "whose bounding box is under min_span_degrees in both directions was left out, so a "
            "small island is open water here, and lakes are land."
        ),
        "rings": rings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=NATURAL_EARTH_110M_LAND, help="GeoJSON file or URL of Natural Earth land.")
    parser.add_argument("--out", required=True, help="Where the outline asset is written.")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, help="Douglas-Peucker tolerance, degrees.")
    parser.add_argument("--min-span", type=float, default=DEFAULT_MIN_SPAN, help="Smallest ring span, degrees, that survives.")
    parser.add_argument("--source-name", default=SOURCE, help="What the asset records as its provenance.")
    arguments = parser.parse_args(argv)

    payload = json.loads(read_bytes(arguments.source))
    rings, counts = outline(rings_of(payload), arguments.tolerance, arguments.min_span)
    path = Path(arguments.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        document(rings, arguments.tolerance, arguments.min_span, arguments.source_name),
        separators=(",", ":"),
    )
    path.write_text(text, encoding="utf-8")

    widest = max((max(point[0] for point in ring) for ring in rings), default=0.0)
    narrowest = min((min(point[0] for point in ring) for ring in rings), default=0.0)
    print(
        f"{len(rings)} of {counts['in']} rings kept ({counts['simplified_out']} simplified away, "
        f"{counts['too_small']} too small), {counts['vertices_in']:,} vertices to "
        f"{counts['vertices_out']:,}, {path.stat().st_size:,} bytes at {path}"
    )
    print(f"longitude runs {narrowest} to {widest}")
    # A ring that straddles the antimeridian between two of its own vertices would draw as a line
    # across the whole map. Antarctica reaches both edges and Afro-Eurasia reaches 180, which is
    # not the same thing as crossing it, so the check is on consecutive vertices rather than span.
    crossing = [len(ring) for ring in rings if any(abs(b[0] - a[0]) > 180 for a, b in zip(ring, ring[1:]))]
    print(f"{len(crossing)} ring(s) cross the antimeridian between two vertices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
