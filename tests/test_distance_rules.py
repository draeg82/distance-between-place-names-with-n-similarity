"""Distance, frozen: kilometres, against figures that can be computed by hand.

Every expectation below comes from one fact and one radius:

* one degree of arc is 1/360th of a great circle, so on a sphere of radius *R* it is
  `pi / 180 x R` kilometres;
* the distance library uses the mean Earth radius `R = 6 371.0088 km`, which gives
  **111.195 km** per degree, **10 007.557 km** for a quarter of a meridian and
  **20 015.114 km** between antipodes.

Stated tolerance: 0.001 km (one metre) for the exact great-circle figures - a hundred
times wider than the rounding in the quoted numbers, and still tight enough to notice a
change of radius, formula or unit. Where the expectation is an approximation of a small
circle (a degree of longitude away from the equator), the tolerance is 0.01 km.
"""

from __future__ import annotations

import math

import pytest

from distnsimilarity.core.distance import great_circle_distance_km
from pins import Spot, pin_distance

EARTH_RADIUS_KM = 6371.0088
ONE_DEGREE_KM = 111.195  # pi / 180 x 6 371.0088, rounded to micrometres
QUARTER_MERIDIAN_KM = 10007.557  # pi / 2 x R
HALF_MERIDIAN_KM = 20015.114  # pi x R
ARC_TOLERANCE_KM = 0.001
SMALL_CIRCLE_TOLERANCE_KM = 0.01

EQUATOR_ORIGIN = Spot("Equator/Prime meridian", 0.0, 0.0)
ONE_DEGREE_NORTH = Spot("1N 0E", 1.0, 0.0)
ONE_DEGREE_EAST = Spot("0N 1E", 0.0, 1.0)
NORTH_POLE = Spot("North pole", 90.0, 0.0)
SOUTH_POLE = Spot("South pole", -90.0, 0.0)
OPPOSITE_MERIDIAN = Spot("0N 180E", 0.0, 180.0)


class TestHandComputedFigures:
    def test_one_degree_of_latitude_is_one_degree_of_arc(self) -> None:
        pin_distance(
            first=EQUATOR_ORIGIN,
            second=ONE_DEGREE_NORTH,
            expected_km=ONE_DEGREE_KM,
            tolerance_km=ARC_TOLERANCE_KM,
        )

    def test_one_degree_of_longitude_at_the_equator_is_the_same_distance(self) -> None:
        pin_distance(
            first=EQUATOR_ORIGIN,
            second=ONE_DEGREE_EAST,
            expected_km=ONE_DEGREE_KM,
            tolerance_km=ARC_TOLERANCE_KM,
        )

    def test_a_quarter_of_a_meridian(self) -> None:
        pin_distance(
            first=EQUATOR_ORIGIN,
            second=NORTH_POLE,
            expected_km=QUARTER_MERIDIAN_KM,
            tolerance_km=ARC_TOLERANCE_KM,
        )

    def test_pole_to_pole_is_half_a_great_circle(self) -> None:
        pin_distance(
            first=SOUTH_POLE,
            second=NORTH_POLE,
            expected_km=HALF_MERIDIAN_KM,
            tolerance_km=ARC_TOLERANCE_KM,
        )

    def test_antipodes_are_half_a_great_circle_apart(self) -> None:
        pin_distance(
            first=EQUATOR_ORIGIN,
            second=OPPOSITE_MERIDIAN,
            expected_km=HALF_MERIDIAN_KM,
            tolerance_km=ARC_TOLERANCE_KM,
        )

    def test_a_place_is_exactly_zero_kilometres_from_itself(self) -> None:
        assert great_circle_distance_km(48.858, 2.294, 48.858, 2.294) == 0.0

    @pytest.mark.parametrize("latitude", [45.0, 60.0])
    def test_a_degree_of_longitude_shrinks_with_the_cosine_of_the_latitude(
        self, latitude: float
    ) -> None:
        """The great circle between two meridian points at the same latitude is a hair
        shorter than the parallel of latitude joining them, which is what the cosine
        gives: hence a wider tolerance than the exact figures above."""
        expected_km = ONE_DEGREE_KM * math.cos(math.radians(latitude))
        pin_distance(
            first=Spot(f"{latitude}N 0E", latitude, 0.0),
            second=Spot(f"{latitude}N 1E", latitude, 1.0),
            expected_km=expected_km,
            tolerance_km=SMALL_CIRCLE_TOLERANCE_KM,
        )


class TestKilometresSpecifically:
    def test_the_magnitude_of_a_degree_is_kilometres(self) -> None:
        one_degree = great_circle_distance_km(0.0, 0.0, 1.0, 0.0)
        assert 100.0 < one_degree < 120.0, (
            f"one degree of arc is {one_degree}: expected roughly 111 kilometres, "
            "not metres (111 000), centimetres (11 100 000) or miles (69)"
        )

    def test_a_metre_expectation_is_caught_as_the_wrong_unit(self) -> None:
        """The tolerance is what catches a unit change: 111 195 km is not within a metre
        of 111.195 km, and the failure says which unit it looks like."""
        with pytest.raises(AssertionError) as wrong_unit:
            pin_distance(
                first=EQUATOR_ORIGIN,
                second=ONE_DEGREE_NORTH,
                expected_km=111_195.0,
                tolerance_km=ARC_TOLERANCE_KM,
            )
        assert "metres" in str(wrong_unit.value)

    def test_a_mile_expectation_is_caught_as_the_wrong_unit(self) -> None:
        with pytest.raises(AssertionError) as wrong_unit:
            pin_distance(
                first=EQUATOR_ORIGIN,
                second=ONE_DEGREE_NORTH,
                expected_km=69.05,
                tolerance_km=ARC_TOLERANCE_KM,
            )
        assert "miles" in str(wrong_unit.value)

    def test_the_distance_is_symmetric_in_the_two_places(self) -> None:
        kyiv = Spot("Kyiv", 50.45, 30.52)
        capetown = Spot("Cape Town", -33.92, 18.42)
        forward = great_circle_distance_km(kyiv.latitude, kyiv.longitude, capetown.latitude, capetown.longitude)
        back = great_circle_distance_km(capetown.latitude, capetown.longitude, kyiv.latitude, kyiv.longitude)
        assert abs(forward - back) < 1e-9, f"{forward} km one way, {back} km the other"


class TestCoordinatesAreChecked:
    @pytest.mark.parametrize("latitude", [90.1, -90.1, 180.0])
    def test_a_latitude_out_of_range_is_refused(self, latitude: float) -> None:
        with pytest.raises(ValueError) as refused:
            great_circle_distance_km(latitude, 0.0, 0.0, 0.0)
        assert str(latitude) in str(refused.value)

    @pytest.mark.parametrize("longitude", [180.1, -180.1, 360.0])
    def test_a_longitude_out_of_range_is_refused(self, longitude: float) -> None:
        with pytest.raises(ValueError) as refused:
            great_circle_distance_km(0.0, longitude, 0.0, 0.0)
        assert str(longitude) in str(refused.value)

    def test_the_second_place_is_checked_too(self) -> None:
        with pytest.raises(ValueError) as refused:
            great_circle_distance_km(0.0, 0.0, 0.0, 999.0)
        assert "second" in str(refused.value)
