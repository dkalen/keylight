"""Solar elevation (NOAA spreadsheet algorithm) and the Sun Schedule.

Accurate to a fraction of a degree, which is far more than a brightness
baseline needs. Standard library only, no network.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

DAWN_ELEVATION = -6.0     # civil twilight: schedule starts rising
FULL_ELEVATION = 20.0     # schedule reaches day brightness


def solar_elevation(when: datetime, latitude: float, longitude: float) -> float:
    """Sun's elevation above the horizon in degrees (no refraction correction)."""
    if when.tzinfo is None:
        raise ValueError("solar_elevation needs an aware datetime")
    utc = when.astimezone(timezone.utc)
    jd = utc.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525

    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m = math.radians(mean_anom)
    center = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * m) * (0.019993 - 0.000101 * jc)
              + math.sin(3 * m) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = mean_long + center - 0.00569 - 0.00478 * math.sin(omega)
    mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))

    y = math.tan(obliq / 2) ** 2
    l0 = math.radians(mean_long)
    eq_time = 4 * math.degrees(y * math.sin(2 * l0) - 2 * ecc * math.sin(m)
                               + 4 * ecc * y * math.sin(m) * math.cos(2 * l0)
                               - 0.5 * y * y * math.sin(4 * l0) - 1.25 * ecc * ecc * math.sin(2 * m))

    minutes = utc.hour * 60 + utc.minute + utc.second / 60
    true_solar = (minutes + eq_time + 4 * longitude) % 1440
    hour_angle = math.radians(true_solar / 4 - 180)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))


def daylight_fraction(elevation: float) -> float:
    """0 at/below civil dusk, 1 at/above FULL_ELEVATION, smoothstep between."""
    t = (elevation - DAWN_ELEVATION) / (FULL_ELEVATION - DAWN_ELEVATION)
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def schedule_brightness(when: datetime, latitude: float | None, longitude: float | None,
                        day: int, night: int) -> int:
    """Sun Schedule baseline. Without a location, falls back to `day`."""
    if latitude is None or longitude is None:
        return day
    f = daylight_fraction(solar_elevation(when, latitude, longitude))
    return round(night + (day - night) * f)
