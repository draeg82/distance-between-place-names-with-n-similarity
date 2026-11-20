"""Great-circle distance between two places, in kilometres."""

from __future__ import annotations

from haversine import Unit, haversine

_LATITUDE_RANGE = (-90.0, 90.0)
_LONGITUDE_RANGE = (-180.0, 180.0)


def out_of_range_coordinate(latitude: float, longitude: float) -> str | None:
    """What is wrong with a pair of coordinates, or None when both are in range.

    The distance function refuses a bad coordinate too, but by then a pair has a name on each
    side and no clue which row the number came from; whoever reads a place table needs the row.
    So the table asks this question while reading, and can say which place it was holding.
    """
    if not _LATITUDE_RANGE[0] <= latitude <= _LATITUDE_RANGE[1]:
        return f"latitude out of range: {latitude}"
    if not _LONGITUDE_RANGE[0] <= longitude <= _LONGITUDE_RANGE[1]:
        return f"longitude out of range: {longitude}"
    return None


def great_circle_distance_km(
    first_latitude: float, first_longitude: float, second_latitude: float, second_longitude: float
) -> float:
    """Kilometres between two coordinates along a great circle.

    The unit is passed explicitly rather than inherited from the library's default, so a
    change of that default cannot silently change the reported distance.
    """
    for latitude, longitude, label in (
        (first_latitude, first_longitude, "first"),
        (second_latitude, second_longitude, "second"),
    ):
        problem = out_of_range_coordinate(latitude, longitude)
        if problem is not None:
            raise ValueError(f"{label} {problem}")
    return haversine(
        (float(first_latitude), float(first_longitude)),
        (float(second_latitude), float(second_longitude)),
        unit=Unit.KILOMETERS,
    )
