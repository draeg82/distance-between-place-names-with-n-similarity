"""The coastline the browser draws its map on: data in the package, and no new dependency.

#8 wants a world map without a mapping library on the Python side and without a page that loads
something from another machine. Both point the same way: the coastline is committed as an asset,
built by `scripts/make_world_outline.py` from public-domain Natural Earth land and served by the
same server as the page. Nothing at run time reads the script, and nothing in the install builds
the asset - the JSON below is the thing that ships.

Because the asset is data, it can be checked rather than looked at. The questions that matter are
whether it says where its numbers came from, whether land is land where a reader can check it by
hand, and whether it stayed small enough to belong in a wheel. The even-odd rule that decides
"inside a ring" is the same one the browser's canvas uses to fill land, so a point that reads as
land here reads as land on the screen.

`scripts/make_world_outline.py` is exercised on a fixture land collection written into `tmp_path`,
never on the network: the committed asset is what it is, and its provenance is in the file.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from http_support import serve
from support import dump_row, place_database

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "src" / "distnsimilarity" / "api" / "static"
OUTLINE = STATIC / "world-outline.json"
TOOL = REPO_ROOT / "scripts" / "make_world_outline.py"

#: Places a reader can look up on any atlas and be sure about: every one of these is well inside
#: a landmass, at a distance no 1:110,000,000 coastline could argue with.
LAND = {
    "Berlin": (13.4, 52.5),
    "Moscow": (37.6, 55.8),
    "Beijing": (116.4, 39.9),
    "Tokyo": (139.7, 35.7),
    "Sydney": (151.2, -33.9),
    "Cape Town": (18.4, -33.9),
    "Rio de Janeiro": (-43.2, -22.9),
    "Santiago": (-70.7, -33.4),
    "Denver": (-105.0, 39.7),
    "Reykjavik": (-21.9, 64.1),
    "Nuuk, Greenland": (-51.7, 64.2),
    "Alice Springs, Australia": (133.9, -23.7),
    "Antananarivo, Madagascar": (47.5, -18.9),
    "Wellington, New Zealand": (174.8, -41.3),
    "Anchorage, Alaska": (-149.9, 61.2),
}

#: Open water, and far enough from any coast that a coarse coastline still reads as water.
WATER = {
    "Gulf of Guinea": (-10.0, 4.0),
    "mid-Pacific": (-150.0, 0.0),
    "Pacific off Peru": (-85.0, -12.0),
    "Indian Ocean": (80.0, -25.0),
    "South Atlantic": (-20.0, -40.0),
    "Southern Ocean south of Africa": (20.0, -60.0),
    "Sargasso Sea": (-40.0, 30.0),
    "Bay of Bengal": (88.0, 15.0),
    "Barents Sea": (40.0, 74.0),
}

#: A city on an island at this resolution is a coin toss, and saying so in a test is better than
#: pretending the outline is an address geocoder. New York sits on the western shore of Hudson
#: County; 1:110m land draws the coast a few kilometres seaward of it.
BEYOND_THIS_RESOLUTION = {
    "New York": (-74.0, 40.7),
    "Istanbul": (29.0, 41.0),
    "Mumbai": (72.8, 19.1),
}

#: The asset is 1,700-odd vertices of coastline. The ceiling is here so that a regeneration at a
#: finer tolerance, or a swap for a 50m source, has to be a decision somebody writes down.
MAX_BYTES = 40_000
MAX_VERTICES = 3_000


def asset() -> dict[str, Any]:
    assert OUTLINE.is_file(), f"the map needs {OUTLINE.name}, which is not in {STATIC}"
    return json.loads(OUTLINE.read_text(encoding="utf-8"))


def rings_of(document: dict[str, Any]) -> list[list[list[float]]]:
    return list(document["rings"])


def is_land(rings: list[list[list[float]]], longitude: float, latitude: float) -> bool:
    """Even-odd rule, the one `ctx.fill()` uses: count the edges a ray crosses on the way east."""
    crossings = 0
    for ring in rings:
        for index in range(len(ring) - 1):
            lon0, lat0 = ring[index]
            lon1, lat1 = ring[index + 1]
            if (lat0 <= latitude < lat1) or (lat1 <= latitude < lat0):
                share = (latitude - lat0) / (lat1 - lat0)
                if lon0 + share * (lon1 - lon0) > longitude:
                    crossings += 1
    return crossings % 2 == 1


def tool():
    """The generator, as a module. It is a script in `scripts/`, not an installed package."""
    spec = importlib.util.spec_from_file_location("make_world_outline", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def land_collection(rings: list[list[list[float]]]) -> dict[str, Any]:
    """A stand-in Natural Earth land collection: one feature per ring, no properties that matter."""
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"featurecla": "Land"}, "geometry": {"type": "Polygon", "coordinates": [ring]}}
            for ring in rings
        ],
    }


class TestTheCommittedOutline:
    def test_the_outline_is_in_the_package_next_to_the_page(self) -> None:
        assert OUTLINE.is_file(), f"the map needs {OUTLINE.name} beside the page it is drawn on"

    def test_the_outline_says_where_its_numbers_came_from(self) -> None:
        document = asset()
        assert document["type"] == "LandOutlines"
        source = document["source"].lower()
        assert "natural earth" in source, document["source"]
        assert "public domain" in source, "shipping coastline data means knowing its licence"
        assert document["tolerance_degrees"] > 0
        assert document["min_span_degrees"] > 0
        assert "closed" in document["note"], "a reader has to know a ring repeats its first vertex"

    def test_every_ring_is_closed_and_on_the_globe(self) -> None:
        for index, ring in enumerate(rings_of(asset())):
            assert len(ring) >= 4, f"ring {index} has too few vertices to be a shape"
            assert ring[0] == ring[-1], f"ring {index} is not closed"
            for longitude, latitude in ring:
                assert -180.0 <= longitude <= 180.0, f"ring {index} is off the globe"
                assert -90.0 <= latitude <= 90.0, f"ring {index} is off the globe"

    def test_the_land_is_where_the_land_is(self) -> None:
        rings = rings_of(asset())
        misplaced = [name for name, point in LAND.items() if not is_land(rings, *point)]
        assert not misplaced, f"these are inland, so the outline is wrong about {misplaced}"

    def test_the_ocean_is_where_the_ocean_is(self) -> None:
        rings = rings_of(asset())
        misplaced = [name for name, point in WATER.items() if is_land(rings, *point)]
        assert not misplaced, f"these are open water, so the outline is wrong about {misplaced}"

    def test_a_city_exactly_on_the_coast_is_beyond_this_resolution(self) -> None:
        """Pinned, not hidden: 1:110m land is a coastline, not an address.

        The three cities below are on the sea rather than near it, and a coastline this coarse
        puts the shore on the seaward side of them. The map is drawn to show a reader which ocean
        an arc crosses, so that is the right trade - but it is a trade, and a test is where a
        trade stays written down.
        """
        rings = rings_of(asset())
        assert [name for name, point in BEYOND_THIS_RESOLUTION.items() if is_land(rings, *point)] == []

    def test_the_outline_is_small_enough_to_ship(self) -> None:
        document = asset()
        bytes_of_it = OUTLINE.stat().st_size
        vertices = sum(len(ring) for ring in rings_of(document))
        assert bytes_of_it <= MAX_BYTES, f"{bytes_of_it:,} bytes of coastline is a dependency in disguise"
        assert vertices <= MAX_VERTICES, f"{vertices:,} vertices is not a coarse outline"

    def test_the_outline_is_served_by_name_from_here(self, tmp_path: Path) -> None:
        database = place_database(
            tmp_path / "places.sqlite3", [dump_row(1, "Aa", 0.0, 0.0, population=1_000)]
        )

        with serve(database) as server:
            response = server.get("/static/world-outline.json")

        assert response.status_code == 200, response.text[:200]
        assert response.headers["content-type"].startswith("application/json"), response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"
        assert rings_of(response.json()) == rings_of(asset())

    def test_the_page_loads_the_outline_from_this_server(self) -> None:
        """The one URL-shaped thing the front end asks for is a path on this same server."""
        sources = "".join(
            (STATIC / name).read_text(encoding="utf-8") for name in ("app.js", "results.js", "map.js")
        )
        assert "world-outline.json" in sources, "the map needs the outline, and says so by name"
        assert '"/static/world-outline.json"' in sources, "the outline is fetched from here, not from somewhere"


class TestTheOutlineTool:
    """The generator has two decisions in it - how far to simplify, and what is too small - and both are testable."""

    def test_simplifying_keeps_the_corners_and_drops_the_midpoints(self) -> None:
        make = tool()
        square = [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [4.0, 2.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]]
        kept = make.simplify(square, tolerance=0.25)
        assert [0.0, 0.0] in kept and [4.0, 4.0] in kept
        assert [2.0, 0.0] not in kept, "a vertex on a straight run of coast is worth nothing"
        assert kept[0] == kept[-1], "a ring goes out closed and comes back closed"

    def test_a_bend_shallower_than_the_tolerance_is_gone(self) -> None:
        make = tool()
        shallow = [[0.0, 0.0], [2.0, 0.08], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0], [0.0, 0.0]]
        assert [2.0, 0.08] not in make.simplify(shallow, tolerance=0.25)
        assert [2.0, 0.08] in make.simplify(shallow, tolerance=0.02), "the tolerance is doing the work"

    def test_a_degree_of_longitude_is_worth_what_it_is_worth_at_that_latitude(self) -> None:
        """A tolerance near a pole must not forgive ten times the error it forgives at the equator."""
        make = tool()
        # A vertex one degree east of a north-south line: worth a degree at the equator, and worth
        # a sixth of one at 81 degrees north, which is what the map has to forgive or keep.
        at_the_equator = make.perpendicular_distance((1.0, 1.0), (0.0, 0.0), (0.0, 2.0))
        near_a_pole = make.perpendicular_distance((1.0, 81.0), (0.0, 80.0), (0.0, 82.0))
        assert 0.99 < at_the_equator < 1.01, at_the_equator
        assert near_a_pole < 0.2 * at_the_equator, (
            f"a degree east of the same line measures {near_a_pole} near a pole and {at_the_equator} at the equator"
        )

    def test_land_smaller_than_the_floor_is_left_out(self) -> None:
        make = tool()
        island = [[0.0, 0.0], [0.4, 0.0], [0.4, 0.4], [0.0, 0.4], [0.0, 0.0]]
        continent = [[10.0, 10.0], [30.0, 10.0], [30.0, 25.0], [10.0, 25.0], [10.0, 10.0]]
        kept, counts = make.outline([island, continent], tolerance=0.25, min_span=1.2)
        assert counts["too_small"] == 1, counts
        assert len(kept) == 1
        assert kept[0][0][0] > 9.0, "the continent is the thing that survived"

    def test_rounding_that_puts_two_vertices_on_one_number_leaves_one_vertex(self) -> None:
        make = tool()
        # Two edges a tenth of a degree apart, which cannot be drawn apart at one decimal place.
        sliver = [[0.0, 0.0], [0.05, 0.0], [0.05, 5.0], [0.0, 5.0], [0.04, 2.5], [0.0, 0.0]]
        kept, _ = make.outline([sliver], tolerance=0.0, min_span=0.0)
        assert kept, "a ring wide enough to keep should still be a ring"
        ring = kept[0]
        assert ring[0] == ring[-1]
        neighbours = list(zip(ring, ring[1:]))
        assert all(one != two for one, two in neighbours), f"nothing is drawn between two identical vertices: {ring}"

    def test_rings_reads_polygons_and_multipolygons_and_ignores_holes(self) -> None:
        make = tool()
        document = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [
                        [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                        [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
                    ]},
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "MultiPolygon", "coordinates": [
                        [[[10, 10], [14, 10], [14, 14], [10, 14], [10, 10]]],
                        [[[20, 20], [24, 20], [24, 24], [20, 24], [20, 20]]],
                    ]},
                },
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}},
            ],
        }
        rings = make.rings_of(document)
        assert len(rings) == 3, "one polygon and two multipolygon parts, and no lake in the middle of a lake"
        assert rings[0][0] == (0.0, 0.0), "numbers come back as longitude and latitude"

    def test_the_tool_writes_the_asset_from_a_land_collection(self, tmp_path: Path) -> None:
        source = tmp_path / "land.geojson"
        source.write_text(
            json.dumps(
                land_collection([[[0.0, 0.0], [9.0, 0.2], [9.0, 9.0], [0.0, 9.0], [0.0, 0.0]]])
            ),
            encoding="utf-8",
        )
        out = tmp_path / "nest" / "world-outline.json"

        finished = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "--source",
                str(source),
                "--out",
                str(out),
                "--tolerance",
                "0.25",
                "--source-name",
                "A fixture, for a test",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert finished.returncode == 0, finished.stderr
        document = json.loads(out.read_text(encoding="utf-8"))
        assert document["source"] == "A fixture, for a test"
        assert document["tolerance_degrees"] == 0.25
        assert document["rings"][0][0] == document["rings"][0][-1]
        assert "rings kept" in finished.stdout, finished.stdout
