"""Great-circle distance between two places, in kilometres."""

from __future__ import annotations

from haversine import Unit, haversine

_LATITUDE_RANGE = (-90.0, 90.0)
_LONGITUDE_RANGE = (-180.0, 180.0)


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
        if not _LATITUDE_RANGE[0] <= latitude <= _LATITUDE_RANGE[1]:
            raise ValueError(f"{label} latitude out of range: {latitude}")
        if not _LONGITUDE_RANGE[0] <= longitude <= _LONGITUDE_RANGE[1]:
            raise ValueError(f"{label} longitude out of range: {longitude}")
    return haversine(
        (float(first_latitude), float(first_longitude)),
        (float(second_latitude), float(second_longitude)),
        unit=Unit.KILOMETERS,
    )
