"""NOAA-style sunrise approximation, offline. Polar nights have no event."""
from datetime import datetime, timedelta, timezone
from math import acos, asin, atan, cos, degrees, radians, sin, tan


def sun_time(day, latitude, longitude, sunset=False):
    lng = longitude / 15
    t = day.timetuple().tm_yday + ((18 if sunset else 6) - lng) / 24
    m = 0.9856 * t - 3.289
    l = (m + 1.916 * sin(radians(m)) + 0.020 * sin(2 * radians(m)) + 282.634) % 360
    ra = degrees(atan(0.91764 * tan(radians(l)))) % 360
    ra += (l // 90) * 90 - (ra // 90) * 90
    ra /= 15
    sd = 0.39782 * sin(radians(l))
    denominator = cos(asin(sd)) * cos(radians(latitude))
    if abs(denominator) < 1e-12:
        return None
    ch = (cos(radians(90.833)) - sd * sin(radians(latitude))) / denominator
    if not -1 <= ch <= 1:
        return None
    h = degrees(acos(ch))
    if not sunset:
        h = 360 - h
    utc_hours = (h / 15 + ra - 0.06571 * t - 6.622 - lng) % 24
    candidate = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=utc_hours)
    # Select the UTC date that gives the requested local solar date.
    solar_date = (candidate + timedelta(hours=lng)).date()
    return candidate + timedelta(days=(day - solar_date).days)
