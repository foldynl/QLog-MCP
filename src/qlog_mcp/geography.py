"""Validated origins and approximate distances to catalog points."""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EARTH_RADIUS_KM = 6371.0088


class NearSearch(BaseModel):
    """Radius in km from a grid centre OR latitude/longitude in decimal degrees."""

    model_config = ConfigDict(extra="forbid")

    radius_km: float = Field(
        gt=0, strict=True, allow_inf_nan=False, description="Search radius in km.",
    )
    grid: str | None = Field(
        default=None,
        pattern=r"^[A-Ra-r]{2}[0-9]{2}(?:[A-Xa-x]{2}(?:[0-9]{2}(?:[A-Xa-x]{2}(?:[0-9]{2})?)?)?)?$",
        description="4–12 character Maidenhead locator; uses its centre.",
    )
    latitude: float | None = Field(
        default=None,
        ge=-90,
        le=90,
        strict=True,
        allow_inf_nan=False,
        description="Origin latitude in decimal degrees.",
    )
    longitude: float | None = Field(
        default=None,
        ge=-180,
        le=180,
        strict=True,
        allow_inf_nan=False,
        description="Origin longitude in decimal degrees.",
    )

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if self.grid is not None:
            if self.latitude is not None or self.longitude is not None:
                raise ValueError("near requires grid OR latitude and longitude, never both")
            self.grid = self.grid.upper()
        elif self.latitude is None or self.longitude is None:
            raise ValueError("near requires grid OR both latitude and longitude")
        return self

    def centre(self) -> tuple[float, float]:
        if self.grid is None:
            # Both coordinates are required by validate_origin.
            assert self.latitude is not None and self.longitude is not None
            return self.latitude, self.longitude
        latitude, longitude = -90.0, -180.0
        height, width = 180.0, 360.0
        for index in range(0, len(self.grid), 2):
            radix = 18 if index == 0 else 10 if index % 4 == 2 else 24
            base = ord("0") if radix == 10 else ord("A")
            height /= radix
            width /= radix
            longitude += (ord(self.grid[index]) - base) * width
            latitude += (ord(self.grid[index + 1]) - base) * height
        return latitude + height / 2, longitude + width / 2

    def bounds(self) -> tuple[float, float, float, float]:
        """Conservative bounding box; west > east means crossing the date line."""
        latitude, longitude = self.centre()
        angle = min(self.radius_km / EARTH_RADIUS_KM, math.pi)
        # Widen the prefilter (~1 m) for roundoff, including near antipodes.
        # The final distance predicate remains unpadded.
        padding = 1e-5
        delta_latitude = math.degrees(angle) + padding
        south, north = max(-90, latitude - delta_latitude), min(90, latitude + delta_latitude)
        if south <= -90 or north >= 90:
            return south, north, -180, 180
        longitude_ratio = math.sin(angle) / math.cos(math.radians(latitude))
        delta_longitude = math.degrees(math.asin(min(1, longitude_ratio))) + padding
        return (
            south,
            north,
            (longitude - delta_longitude + 180) % 360 - 180,
            (longitude + delta_longitude + 180) % 360 - 180,
        )

    def effective(self) -> dict[str, str | float]:
        latitude, longitude = self.centre()
        result: dict[str, str | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": self.radius_km,
        }
        if self.grid is not None:
            result["grid"] = self.grid
        return result


def distance_km(
    origin_latitude: float,
    origin_longitude: float,
    latitude: object,
    longitude: object,
) -> float | None:
    """Great-circle distance, or NULL for missing/invalid directory coordinates."""
    if (
        not isinstance(latitude, (int, float))
        or not math.isfinite(latitude)
        or not isinstance(longitude, (int, float))
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    latitude1, latitude2 = math.radians(origin_latitude), math.radians(latitude)
    delta_latitude = latitude2 - latitude1
    delta_longitude = math.radians(longitude - origin_longitude)
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude1) * math.cos(latitude2) * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1, max(0, haversine))))
