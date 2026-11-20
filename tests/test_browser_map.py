"""The map: where a pair is drawn, how an arc bends, and what the page may not decide for itself.

`map.js` is the only file in the front end with trigonometry in it, and it is there for one reason:
a great circle is a curve on a plate carrée map, and the curve is the finding. That exemption is
narrow on purpose - the file has no Earth radius in it and no kilometres in it, so the distance a
reader reads can only have come from `/v1/search`. `TestTheMapAddsNothing` holds that line, and
`tests/test_browser_app.py` holds the wider ban over the rest of the page.

Everything else here is about placement, which is testable without a browser because the two
interesting parts are pure: the sampling of an arc, and the list of things a canvas is asked to do.
The painter is checked with a context that writes down its instructions, so "the marker of a place
at 90 east is three quarters of the way across" is an assertion about numbers, and "a search that
found nothing leaves no arc behind" is an assertion about a drawing rather than about a hope.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from browser_support import (
    MAP_BANNED,
    MAP_JS,
    OUTLINE,
    asked,
    in_map,
    in_presentation,
    page_assets,
    paint_scene,
)
from http_support import events_of, serve
from support import dump_row, place_database

WIDTH = 1000
HEIGHT = 500


class TestThePageCarriesTheMap:
    """The adapter's side of the map: frames, picks, and nothing stale left behind."""

    def script(self) -> str:
        return page_assets()["app.js"]

    def test_the_arc_draws_itself_in_on_frames_and_never_on_a_timer(self) -> None:
        script = self.script()
        assert "requestAnimationFrame(" in script, "frames, so the drawing keeps step with the screen"
        assert "cancelAnimationFrame(" in script, "and a better list mid-draw restarts one arc, not two"
        assert "setInterval" not in script, (
            "a timer would keep redrawing a map that has nothing new to show"
        )

    def test_a_person_who_has_asked_for_no_motion_gets_the_finished_arc(self) -> None:
        script = self.script()
        assert 'matchMedia("(prefers-reduced-motion: reduce)")' in script, (
            "CSS cannot stop a canvas being painted, so the arc asks here as well"
        )
        assert "prefers-reduced-motion" in page_assets()["styles.css"]

    def test_a_row_can_be_picked_with_the_mouse_or_the_keyboard(self) -> None:
        script = self.script()
        for token in ['addEventListener("click"', 'addEventListener("keydown"', '"Enter"', "tabIndex"]:
            assert token in script, f"a row is not reachable without {token}"
        assert "dataset.index" in script, "the pick says which row it landed on"

    def test_the_map_is_redrawn_from_the_list_as_it_improves(self) -> None:
        block = function_body(self.script(), "showList")
        assert "finds = event.results" in block
        assert "drawInFocus()" in block, (
            "the arcs come from the same `results` events as the rows, not from a second request"
        )

    def test_a_new_search_clears_the_map_before_anything_arrives(self) -> None:
        block = function_body(self.script(), "clearRows")
        assert "finds = []" in block
        assert "drawMap()" in block, "the arcs of the search before this one are not this one's answer"

    def test_a_search_that_found_nothing_leaves_no_arc_behind(self) -> None:
        script = self.script()
        where_done = script[script.index('event.type === "done"') :]
        assert "clearRows()" in where_done[: where_done.index("return;")], (
            "an empty result clears the map rather than leaving last search's pair on it"
        )

    def test_the_canvas_is_only_resized_when_the_box_it_was_given_changes(self) -> None:
        assert "if (mapCanvas.width !== width || mapCanvas.height !== height)" in self.script(), (
            "resizing a canvas wipes it, and a `results` event every few hundred milliseconds "
            "should not be seen by the compositor at all"
        )

    def test_the_words_beside_the_map_come_from_the_presentation_module(self) -> None:
        script = self.script()
        assert "focusWords(" in script
        assert "toFixed" not in script, "the page formats nothing: numbers are worded in results.js"

    def test_the_caption_knows_whether_a_search_has_run(self) -> None:
        block = function_body(self.script(), "showCaption")
        assert "echo === null" in block, (
            "a page nobody has searched yet and a search that found nothing are different answers"
        )

    def test_the_page_says_what_the_caption_will_say_before_any_script_runs(self) -> None:
        words = re.search(r'const NOTHING_SEARCHED_YET = "([^"]+)"', self.script()).group(1)
        assert words in page_assets()["index.html"], (
            "the caption should not be a flash of different wording once the module loads"
        )


def function_body(script: str, name: str) -> str:
    """The text of one function, so a test can say where a call happens rather than that it exists."""
    start = script.index(f"function {name}(")
    end = script.index("\n}", start)
    return script[start:end]


def place(
    asciiname: str,
    latitude: float,
    longitude: float,
    *,
    country: str = "XX",
    population: int = 10_000,
    geonameid: int = 1,
) -> dict[str, Any]:
    """A place as the wire sends it, which is the only shape this module is allowed to know."""
    return {
        "geonameid": geonameid,
        "asciiname": asciiname,
        "display_name": asciiname,
        "latitude": latitude,
        "longitude": longitude,
        "population": population,
        "country_code": country,
    }


def a_row(
    first: tuple[str, float, float],
    second: tuple[str, float, float],
    *,
    distance: float = 1_000.0,
    difference: int = 1,
) -> dict[str, Any]:
    """One find, as a `results` event sends it: two places and the API's own answers about them."""
    name, latitude, longitude = first
    other, other_latitude, other_longitude = second
    return {
        "first": place(name, latitude, longitude, geonameid=1),
        "second": place(other, other_latitude, other_longitude, geonameid=2),
        "distance_km": distance,
        "character_difference": difference,
        "differing_positions": [0],
    }


def latitudes(arc: list[dict[str, float]]) -> list[float]:
    return [point["latitude"] for point in arc]


def finite(arc: list[dict[str, float]]) -> bool:
    return all(point["longitude"] == point["longitude"] and point["latitude"] == point["latitude"] for point in arc)


class TestTheProjection:
    """AC: "marker and arc placement are correct for a known pair"."""

    def test_a_place_lands_where_arithmetic_puts_it(self) -> None:
        cases = [
            [{"longitude": 0.0, "latitude": 0.0}],
            [{"longitude": -180.0, "latitude": 90.0}],
            [{"longitude": 180.0, "latitude": -90.0}],
            [{"longitude": 13.4, "latitude": 52.5}],
        ]
        assert in_map("project", cases) == [
            {"x": 0.5, "y": 0.5},
            {"x": 0.0, "y": 0.0},
            {"x": 1.0, "y": 1.0},
            {"x": 193.4 / 360, "y": 37.5 / 180},
        ]

    def test_the_bold_arc_is_painted_at_the_coordinates_it_was_given(self) -> None:
        """A pair a reader can check with a ruler: the equator, and two quarters of the map."""
        row = a_row(("Aaaa", 0.0, 0.0), ("Baaa", 0.0, 90.0))

        calls = paint_scene([row], width=WIDTH, height=HEIGHT)

        markers = asked(calls, "arc")
        assert [call[:2] for call in markers] == [
            [WIDTH / 2, HEIGHT / 2],
            [WIDTH * 0.75, HEIGHT / 2],
        ], f"a place on the equator at 0 goes mid-map, and one at 90 east three quarters across: {markers}"

    def test_the_marker_of_a_real_find_sits_where_the_wire_says(self, tmp_path: Path) -> None:
        """The bridge from the API's field names to a dot is checked against a real search."""
        database = place_database(
            tmp_path / "places.sqlite3",
            [
                dump_row(1, "Analac", 10.5, -64.25, population=90_000),
                dump_row(2, "Bnalac", -8.75, 116.5, population=80_000),
            ],
        )

        with serve(database) as server:
            rows = [
                row
                for event in events_of(server.search(1))
                if event.get("type") == "results"
                for row in event["results"]
            ]

        assert len(rows) == 1, rows
        [scene] = in_map("sceneOf", [[{"rows": rows}]])
        assert len(scene["markers"]) == 2
        for marker, found in zip(scene["markers"], (rows[0]["first"], rows[0]["second"])):
            assert marker["x"] == pytest.approx((found["longitude"] + 180) / 360)
            assert marker["y"] == pytest.approx((90 - found["latitude"]) / 180)
            assert marker["name"] == found["display_name"]


class TestTheArc:
    def test_an_arc_bends_polewards_rather_than_running_straight(self) -> None:
        """New York to Tokyo goes over the pole-ward side of the world, not along a latitude line."""
        new_york = {"longitude": -74.0, "latitude": 40.7}
        tokyo = {"longitude": 139.7, "latitude": 35.7}

        [arc] = in_map("arcPoints", [[new_york, tokyo, 96]])

        assert arc[0]["longitude"] == pytest.approx(-74.0, abs=1e-6)
        assert arc[-1]["longitude"] == pytest.approx(139.7, abs=1e-4)
        assert max(latitudes(arc)) > 55.0, (
            f"the great circle reaches {max(latitudes(arc))} north, which is not a bend"
        )
        assert min(latitudes(arc)) < 41.0, "and it does not wander the other way either"

    def test_a_straight_line_across_the_map_is_not_the_arc(self) -> None:
        """The point of drawing an arc rather than a line: the same pair, measured on the map."""
        lines = in_map("arcLines", [[{"longitude": -74.0, "latitude": 40.7}, {"longitude": 139.7, "latitude": 35.7}]])[0]
        piece = max(lines, key=len)
        start, end = piece[0], piece[-1]
        furthest = max(
            abs((point["y"] - start["y"]) - (end["y"] - start["y"]) * (point["x"] - start["x"]) / (end["x"] - start["x"]))
            for point in piece
        )
        assert furthest > 0.02, f"the drawn route is {furthest} of a map off the straight line"

    def test_an_arc_that_crosses_the_antimeridian_arrives_at_the_other_side(self) -> None:
        """AC: "sane for a pair whose arc crosses the antimeridian"."""
        wellington = {"longitude": 174.8, "latitude": -41.3}
        new_york = {"longitude": -74.0, "latitude": 40.7}

        lines = in_map("arcLines", [[wellington, new_york]])[0]

        assert len(lines) == 2, f"one piece leaves the right edge and one arrives at the left, got {len(lines)}"
        assert all(0.0 <= point["x"] <= 1.0 for piece in lines for point in piece), (
            "nothing may be drawn outside the world: it would streak across the map"
        )
        left, right = sorted(lines, key=lambda piece: piece[0]["x"])
        assert right[0]["x"] == pytest.approx(1.0, abs=1e-9) or right[-1]["x"] == pytest.approx(1.0, abs=1e-9)
        assert left[0]["x"] == pytest.approx(0.0, abs=1e-9) or left[-1]["x"] == pytest.approx(0.0, abs=1e-9)
        where_it_crossed = min(right, key=lambda point: abs(point["x"] - 1.0))["y"]
        where_it_lands = min(left, key=lambda point: point["x"])["y"]
        assert where_it_crossed == pytest.approx(where_it_lands, abs=0.01), (
            "the arc continues at the same latitude rather than teleporting"
        )

    def test_places_exactly_opposite_each_other_go_over_the_nearer_pole(self) -> None:
        """Every route between antipodes is the same length, so `asin` alone would pick one at random."""
        [arc] = in_map("arcPoints", [[{"longitude": 0.0, "latitude": 45.0}, {"longitude": 180.0, "latitude": -45.0}, 96]])

        assert finite(arc), json.dumps(arc[:4])
        assert max(latitudes(arc)) == pytest.approx(90.0, abs=1e-6), (
            "every route between antipodes is the same length, so this one is chosen over a pole"
        )

        [almost] = in_map("arcPoints", [[{"longitude": 0.0, "latitude": 45.0}, {"longitude": 179.9, "latitude": -45.001}, 96]])
        assert finite(almost) and len(almost) == 97, "a hair off antipodal is an ordinary arc, not a NaN"

        pieces = in_map("arcLines", [[{"longitude": 0.0, "latitude": 45.0}, {"longitude": 180.0, "latitude": -45.0}]])[0]
        assert all(0.0 <= point["x"] <= 1.0 for piece in pieces for point in piece), (
            "and the chosen route still cannot be drawn outside the world"
        )

    def test_a_pair_nearly_antipodal_still_gets_an_arc_inside_the_world(self) -> None:
        """Araya and Praya are 19,799 km apart: the longest kind of arc this tool finds."""
        araya = {"longitude": -64.25548, "latitude": 10.57998}
        praya = {"longitude": 116.27036, "latitude": -8.70536}

        [arc] = in_map("arcPoints", [[araya, praya, 96]])
        pieces = in_map("arcLines", [[araya, praya]])[0]

        assert finite(arc) and len(arc) == 97
        assert pieces, "a pair that far apart still has a route drawn"
        assert all(0.0 <= point["x"] <= 1.0 for piece in pieces for point in piece)

    def test_the_same_place_twice_is_a_line_of_nothing_rather_than_not_a_number(self) -> None:
        [arc] = in_map("arcPoints", [[{"longitude": 12.0, "latitude": 34.0}, {"longitude": 12.0, "latitude": 34.0}]])

        assert finite(arc) and len(arc) == 2
        assert arc[0]["longitude"] == pytest.approx(arc[1]["longitude"])


class TestTheRunningList:
    """AC: the leading find bold, the rest faint behind, animating as the list improves."""

    def rows(self) -> list[dict[str, Any]]:
        return [
            a_row(("Aaaa", 10.0, -20.0), ("Baaa", -10.0, 40.0)),
            a_row(("Cccc", 20.0, 0.0), ("Dccc", -20.0, 60.0)),
            a_row(("Eeee", 30.0, 10.0), ("Feee", -30.0, 70.0)),
        ]

    def test_the_leading_find_is_the_one_drawn_boldly(self) -> None:
        [scene] = in_map("sceneOf", [[{"rows": self.rows()}]])

        assert [arc["role"] for arc in scene["arcs"]] == ["focus", "trail", "trail"]
        assert len(scene["markers"]) == 2
        assert scene["empty"] is False

    def test_picking_a_row_moves_the_bold_arc_and_leaves_the_order_alone(self) -> None:
        [picked] = in_map("sceneOf", [[{"rows": self.rows(), "focus": 2}]])

        assert [arc["role"] for arc in picked["arcs"]] == ["trail", "trail", "focus"]
        assert len(picked["arcs"]) == 3, "the map draws the ranking it was given, in that order"
        assert [marker["name"] for marker in picked["markers"]] == ["Eeee", "Feee"]

    def test_a_search_that_found_nothing_has_no_arc_to_draw(self) -> None:
        """AC: "a zero-result search leaves the map in a clear empty state rather than a stale arc"."""
        [scene] = in_map("sceneOf", [[{}]])
        calls = paint_scene([], width=WIDTH, height=HEIGHT)

        assert scene == {"arcs": [], "markers": [], "empty": True}
        assert asked(calls, "arc") == [], "no markers"
        assert asked(calls, "stroke") == [[]], "the graticule and nothing else: no stale arc"
        assert asked(calls, "fillRect"), "and the map is still a map: the sea is drawn"

    def test_an_arc_draws_itself_in_as_the_frame_advances(self) -> None:
        araya = {"longitude": -64.25548, "latitude": 10.57998}
        praya = {"longitude": 116.27036, "latitude": -8.70536}

        whole, half, nothing = in_map(
            "arcLines", [[araya, praya, 1.0], [araya, praya, 0.5], [araya, praya, 0.0]]
        )

        assert nothing == []
        assert sum(len(piece) for piece in half) < sum(len(piece) for piece in whole)
        assert half[0][0] == whole[0][0], "it starts where the arc starts, so it grows rather than jumps"
        assert sum(len(piece) for piece in half) > 1, "and half of it is still a line"

    def test_only_the_focused_arc_is_animating(self) -> None:
        [scene] = in_map("sceneOf", [[{"rows": self.rows(), "focus": 1, "share": 0.5}]])

        drawn = [sum(len(piece) for piece in arc["lines"]) for arc in scene["arcs"]]
        assert drawn[1] < drawn[0] and drawn[1] < drawn[2], (
            f"the trailing finds stay whole while the focus draws in: {drawn}"
        )

    def test_the_trailing_finds_are_behind_the_lead_rather_than_over_it(self) -> None:
        calls = paint_scene(self.rows(), width=WIDTH, height=HEIGHT)

        styles = [arguments[0] for name, arguments in calls if name == "=strokeStyle"]
        faint = [index for index, style in enumerate(styles) if style.startswith("rgba(")]
        bold = [index for index, style in enumerate(styles) if style == "#c0392b"]
        assert faint and bold, styles
        assert max(faint) < min(bold), "the faint arcs are laid down first, so the lead is on top"

    def test_the_land_is_filled_as_well_as_outlined(self) -> None:
        square = [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]]

        paths = in_map("landPaths", [[square]])[0]
        calls = paint_scene(self.rows(), paths=paths, width=WIDTH, height=HEIGHT)

        assert asked(calls, "closePath"), "each ring is a shape"
        assert ["evenodd"] in asked(calls, "fill"), (
            "land is filled by the even-odd rule, the one the outline test uses to say what land is"
        )


class TestTheMapAddsNothing:
    def test_the_map_never_turns_coordinates_into_a_distance(self) -> None:
        source = MAP_JS.read_text(encoding="utf-8")
        found = [token for token in MAP_BANNED if token in source]
        assert found == [], (
            f"map.js contains {found}: an Earth radius or a `distance_km` in here is a second "
            "answer to how far apart two places are"
        )

    def test_the_only_sort_in_the_map_orders_pieces_of_an_arc(self) -> None:
        source = MAP_JS.read_text(encoding="utf-8")
        assert re.findall(r"([A-Za-z_]+)\.sort\(", source) == ["lines"], (
            "anything else being sorted is the running list, which the API ranked"
        )

    def test_the_map_module_names_the_outline_it_needs_and_no_other_url(self) -> None:
        source = MAP_JS.read_text(encoding="utf-8")
        assert '"/static/world-outline.json"' in source
        assert re.search(r"https?://", source) is None, "the coastline comes from this server"
        assert OUTLINE.is_file(), "and it is really here"

    def test_the_map_is_a_module_the_page_imports_rather_than_a_second_app(self) -> None:
        script = page_assets()["app.js"]
        assert 'from "./map.js"' in script, "the page asks the map module to draw"
        assert "getContext" in script, "the page owns the canvas element and hands the painter a context"
        assert "getContext" not in MAP_JS.read_text(encoding="utf-8"), (
            "the map module does not go looking for the document: it is given a context, which is "
            "what makes it testable with a context that writes down its instructions"
        )


class TestTheFocusedPair:
    """AC: "both places' coordinates are visible for a selected result"."""

    def test_the_caption_names_both_places_and_prints_both_coordinates(self) -> None:
        row = a_row(("Araya", 10.57998, -64.25548), ("Praya", -8.70536, 116.27036))

        [words] = in_presentation("focusWords", [[row]])

        assert "Araya" in words and "Praya" in words
        assert "10.580, -64.255" in words, words
        assert "-8.705, 116.270" in words, words

    def test_a_caption_for_nothing_says_so(self) -> None:
        [words] = in_presentation("focusWords", [[None]])
        assert words == "", f"an empty list has no caption of its own: {words!r}"

    def test_the_page_has_a_place_for_the_map_and_a_place_for_its_words(self) -> None:
        page = page_assets()["index.html"]
        assert '<canvas id="world-map"' in page, "the map is a canvas, drawn by this server's own script"
        assert 'id="arc-caption"' in page, "the focused pair's coordinates are text on the page, not paint"
        assert "<canvas" not in page.split("<canvas", 2)[1], "one map, not one per find"
