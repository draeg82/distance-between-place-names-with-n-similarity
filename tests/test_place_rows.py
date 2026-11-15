"""What a dump row becomes, and what a bad row does.

The fixtures are one and two rows, written in the real GeoNames column order (19
tab-separated fields, UTF-8), so every expected value can be read off the row that
produced it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable

import pytest

from distnsimilarity.core.places import Place, PlaceRowError, read_places, read_places_lenient
from support import dump_row, dump_text, write_dump


def one(tmp_path: Path, *rows: dict[str, Any]) -> list[Place]:
    """Read the given rows back exactly as the tool will see them."""
    return read_places(write_dump(tmp_path / "cities1.txt", list(rows)))


def with_a_column_removed(row: dict[str, Any]) -> str:
    """A row that lost one of its 19 fields: the shape the loader must refuse."""
    return "\t".join(dump_text([row]).rstrip("\n").split("\t")[:-1])


def with_field(field: str, value: str) -> Callable[[dict[str, Any]], str]:
    def replace(row: dict[str, Any]) -> str:
        row[field] = value
        return dump_text([row])

    return replace


class TestAPlaceIsWhatItsRowSays:
    def test_the_ascii_name_column_is_the_name_the_place_carries(self, tmp_path: Path) -> None:
        rows = [
            dump_row(
                1234567,
                "München",
                48.13715,
                11.57612,
                name="Мюнхен",
                alternatenames="Munich,Monoheimo",
                population=1488202,
                countrycode="DE",
            )
        ]
        place = one(tmp_path, *rows)[0]
        assert place.geonameid == 1234567
        assert place.asciiname == "München", "column 3 is the ASCII name: the one that is compared"
        assert place.display_name == "Мюнхен", "column 2 is carried for display, never compared"
        assert place.latitude == pytest.approx(48.13715)
        assert place.longitude == pytest.approx(11.57612)
        assert place.population == 1488202
        assert place.country_code == "DE"

    def test_a_place_carries_no_alternate_names_at_all(self, tmp_path: Path) -> None:
        """Alternate names are not carried, so they cannot leak into a comparison."""
        assert {field.name for field in dataclasses.fields(Place)} == {
            "geonameid",
            "asciiname",
            "display_name",
            "latitude",
            "longitude",
            "population",
            "country_code",
        }

    def test_the_name_length_a_matcher_needs_comes_from_the_ascii_name(self, tmp_path: Path) -> None:
        place = one(tmp_path, dump_row(1, "São Paulo", -23.55033, -46.63391))[0]
        assert place.name_length == 9 == len(place.asciiname)

    def test_a_blank_population_is_zero_rather_than_an_error(self, tmp_path: Path) -> None:
        row = dump_row(2, "Nowhere", 0.0, 0.0, population=0)
        row["population"] = ""
        assert one(tmp_path, row)[0].population == 0

    def test_places_come_back_in_the_order_of_the_file(self, tmp_path: Path) -> None:
        rows = [
            dump_row(1, "Aa", 0.0, 0.0),
            dump_row(2, "Bb", 1.0, 1.0),
            dump_row(3, "Cc", 2.0, 2.0),
        ]
        assert [place.asciiname for place in one(tmp_path, *rows)] == ["Aa", "Bb", "Cc"]

    def test_a_trailing_blank_line_is_not_a_row(self, tmp_path: Path) -> None:
        path = tmp_path / "cities1.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_text([dump_row(1, "Aa", 0.0, 0.0)]) + "\n\n", encoding="utf-8")
        assert [place.asciiname for place in read_places(path)] == ["Aa"]


class TestUnreadableRowsFailLoudly:
    """A dump that cannot be read is an error, named: the loader used to let pandas coerce
    whatever it liked and carry on."""

    @pytest.mark.parametrize(
        "mangle,expect_in_message",
        [
            (with_a_column_removed, "19 tab-separated fields"),
            (with_field("latitude", "N/A"), "latitude"),
            (with_field("longitude", "west"), "longitude"),
            (with_field("population", "many"), "population"),
            (with_field("geonameid", "not-a-number"), "geonameid"),
            (with_field("latitude", "91.5"), "out of range"),
            (with_field("longitude", "-180.5"), "out of range"),
        ],
    )
    def test_a_bad_row_is_named_by_line_number(
        self, tmp_path: Path, mangle: Callable[[dict[str, Any]], str], expect_in_message: str
    ) -> None:
        good = dump_row(1, "Aa", 0.0, 0.0)
        bad = dump_row(2, "Bb", 1.0, 1.0)
        path = tmp_path / "cities1.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_text([good]) + mangle(bad) + "\n", encoding="utf-8")

        with pytest.raises(PlaceRowError) as refused:
            read_places(path)

        message = str(refused.value)
        assert message.startswith("line 2:"), f"the fault is not attributed to line 2: {message}"
        assert expect_in_message in message, message

    def test_no_places_at_all_come_back_from_a_refused_dump(self, tmp_path: Path) -> None:
        path = write_dump(tmp_path / "cities1.txt", [dump_row(1, "Aa", 0.0, 0.0)])
        path.write_text(path.read_text(encoding="utf-8") + "1\tBb\n", encoding="utf-8")
        with pytest.raises(PlaceRowError):
            assert read_places(path) == []


class TestNothingIsSilentlyDropped:
    def _dump_with_one_bad_row(self, tmp_path: Path) -> Path:
        path = tmp_path / "cities1.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            dump_text([dump_row(1, "Aa", 0.0, 0.0)])
            + with_field("latitude", "somewhere") (dump_row(2, "Bb", 1.0, 1.0))
            + dump_text([dump_row(3, "Cc", 2.0, 2.0)]),
            encoding="utf-8",
        )
        return path

    def test_the_lenient_reader_keeps_the_rows_on_either_side_of_a_bad_one(
        self, tmp_path: Path
    ) -> None:
        places, problems = read_places_lenient(self._dump_with_one_bad_row(tmp_path))
        assert [place.asciiname for place in places] == ["Aa", "Cc"]
        assert len(problems) == 1

    def test_the_lenient_reader_says_which_line_it_refused_and_what_it_said(
        self, tmp_path: Path
    ) -> None:
        _places, problems = read_places_lenient(self._dump_with_one_bad_row(tmp_path))
        problem = problems[0]
        assert problem.line_number == 2
        assert "latitude" in problem.reason
        assert "somewhere" in problem.content

    def test_the_strict_reader_refuses_the_row_the_lenient_reader_names(
        self, tmp_path: Path
    ) -> None:
        path = self._dump_with_one_bad_row(tmp_path)
        _places, problems = read_places_lenient(path)
        assert [problem.line_number for problem in problems] == [2]
        with pytest.raises(PlaceRowError) as refused:
            read_places(path)
        assert str(refused.value).startswith("line 2:")
