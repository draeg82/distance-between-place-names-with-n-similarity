"""What a scan reports: which field it compares, which pairs it reports, in what order.

The scan is the seam the API and the browser will sit on, so its output shape is pinned
here - pair by pair, by hand.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from distnsimilarity.core.matching import differing_positions
from distnsimilarity.core.places import Place, read_places
from distnsimilarity.core.scan import MatchedPair, scan
from pins import NamePair, pin_match, pin_pairs
from support import dump_row, write_dump


def places_from(tmp_path: Path, *rows: dict[str, Any]) -> list[Place]:
    return read_places(write_dump(tmp_path / "cities3.txt", list(rows)))


def names_of(pairs: list[MatchedPair]) -> list[tuple[str, str]]:
    return [(pair.first.asciiname, pair.second.asciiname) for pair in pairs]


class TestMatchingUsesTheAsciiNameField:
    def test_an_ascii_name_match_is_reported_even_when_the_display_names_are_unrelated(
        self, tmp_path: Path
    ) -> None:
        places = places_from(
            tmp_path,
            dump_row(1, "Abc", 10.0, 20.0, name="城市甲"),
            dump_row(2, "Abd", 30.0, 40.0, name="城市乙"),
        )
        pin_pairs(scan(places, 1), at=1, expected=[("Abc", "Abd")])

    def test_a_display_name_match_is_not_reported(self, tmp_path: Path) -> None:
        """The display names are one character apart; the ASCII names are different
        lengths, and nothing about a display name makes a pair comparable."""
        first = dump_row(1, "Vv", 0.0, 0.0, name="Abc")
        second = dump_row(2, "Www", 1.0, 1.0, name="Abd")
        assert differing_positions("Abc", "Abd") == 1, "the fixture: display names are one apart"
        pin_match(NamePair("Abc", "Abd"), at=1, expected=True)

        places = places_from(tmp_path, first, second)
        pin_pairs(scan(places, 1), at=1, expected=[])

    def test_alternate_names_are_never_compared(self, tmp_path: Path) -> None:
        """`Qq` appears in the second row's alternate names. Neither at the threshold that
        would select it nor at any other is it a pair."""
        places = places_from(
            tmp_path,
            dump_row(1, "Qq", 0.0, 0.0, alternatenames="Zzr,Other"),
            dump_row(2, "Rr", 1.0, 1.0, alternatenames="Qq"),
        )
        pin_pairs(scan(places, 1), at=1, expected=[])
        cross_place_pairs = [
            pair for pair in scan(places, 0) if pair.first.geonameid != pair.second.geonameid
        ]
        assert names_of(cross_place_pairs) == [], (
            "n=0 selected a pair whose ASCII names are not equal: "
            f"{names_of(cross_place_pairs)} (the alternate names were compared)"
        )


class TestEveryUnorderedPairIsReportedOnce:
    def test_a_pair_is_reported_under_one_ordering_only(self, tmp_path: Path) -> None:
        places = places_from(
            tmp_path,
            dump_row(1, "Aba", 0.0, 0.0),
            dump_row(2, "Abe", 1.0, 0.0),
            dump_row(3, "Zed", 2.0, 0.0),
        )
        reported = list(scan(places, 1))
        pin_pairs(reported, at=1, expected=[("Aba", "Abe")])
        assert ("Abe", "Aba") not in names_of(reported)

    def test_the_first_place_of_a_pair_is_the_one_that_comes_first_in_the_file(
        self, tmp_path: Path
    ) -> None:
        forward = places_from(
            tmp_path,
            dump_row(1, "Aba", 0.0, 0.0),
            dump_row(2, "Abe", 1.0, 0.0),
        )
        reverse = places_from(
            tmp_path,
            dump_row(2, "Abe", 1.0, 0.0),
            dump_row(1, "Aba", 0.0, 0.0),
        )
        assert names_of(list(scan(forward, 1))) == [("Aba", "Abe")]
        assert names_of(list(scan(reverse, 1))) == [("Abe", "Aba")]

    def test_two_places_sharing_a_name_are_two_places(self, tmp_path: Path) -> None:
        """Distinct records, same ASCII name: `Aba`/`Aba` is a pair at n=0, and the pair's
        distance is the distance between the two places, not zero."""
        places = places_from(
            tmp_path,
            dump_row(1, "Aba", 0.0, 0.0),
            dump_row(2, "Aba", 1.0, 0.0),
        )
        reported = [pair for pair in scan(places, 0) if pair.first.geonameid != pair.second.geonameid]
        assert len(reported) == 1
        assert reported[0].distance_km > 0.0


class TestAPlaceIsComparedWithItself:
    def test_a_place_pairs_with_itself_at_zero_and_reports_no_distance(
        self, tmp_path: Path
    ) -> None:
        places = places_from(tmp_path, dump_row(1, "Abc", 52.52, 13.405))
        reported = list(scan(places, 0))
        assert names_of(reported) == [("Abc", "Abc")]
        assert reported[0].distance_km == 0.0

    def test_a_place_does_not_pair_with_itself_at_a_non_zero_threshold(
        self, tmp_path: Path
    ) -> None:
        places = places_from(tmp_path, dump_row(1, "Abc", 52.52, 13.405))
        assert names_of(list(scan(places, 1))) == []


class TestTheScanCarriesTheDistance:
    def test_the_distance_of_a_reported_pair_is_the_great_circle_between_those_two_places(
        self, tmp_path: Path
    ) -> None:
        """One degree of latitude is 111.195 km, so this pair's distance is derivable
        before the code is run."""
        places = places_from(
            tmp_path,
            dump_row(1, "Aba", 0.0, 0.0),
            dump_row(2, "Abe", 1.0, 0.0),
        )
        reported = list(scan(places, 1))
        assert len(reported) == 1
        distance_km = reported[0].distance_km
        assert abs(distance_km - 111.195) < 0.001, (
            f'("Aba" at 0,0) / ("Abe" at 1,0) is {distance_km} km away; expected 111.195 km '
            "within a metre - one degree of arc on a 6 371.0088 km sphere"
        )

    def test_the_scan_is_a_stream_not_a_list(self, tmp_path: Path) -> None:
        """The API streams as the scan runs, so a scan must hand over pairs lazily."""
        places = places_from(
            tmp_path,
            dump_row(1, "Aba", 0.0, 0.0),
            dump_row(2, "Abe", 1.0, 0.0),
        )
        assert isinstance(scan(places, 1), types.GeneratorType)
