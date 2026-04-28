import math
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import HTTPException

from config import AC_TRANSIT_TIMEZONE, METERS_PER_DEGREE_LAT

def _get_stripped(d: dict, key: str) -> Optional[str]:
    """Return d[key] stripped, or None if missing/empty/whitespace."""
    return ((d.get(key) or "").strip()) or None


def _get_any(d: dict, *keys: str) -> Optional[str]:
    """Return the first non-empty stripped value among d[keys], or None."""
    for key in keys:
        value = _get_stripped(d, key)
        if value:
            return value
    return None


def _try_int(value: Any) -> Optional[int]:
    """Best-effort int conversion. Returns None on failure or empty input."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _time_to_seconds(hours: int, minutes: int, seconds: int) -> int:
    return hours * 3600 + minutes * 60 + seconds


def _route_label(route_row: dict, fallback_id: str) -> str:
    short = _get_stripped(route_row, "route_short_name")
    return short or fallback_id


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _text_tokens(value: Optional[str]) -> set[str]:
    return {
        token
        for token in _normalize_text(value).split()
        if len(token) >= 3 and token not in {"the", "and", "via", "toward", "towards", "route"}
    }


def _parse_departure(value: str) -> datetime:
    departure = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if departure.tzinfo is None:
        return departure.replace(tzinfo=timezone.utc)
    return departure


def _ac_transit_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(AC_TRANSIT_TIMEZONE)
    if now.tzinfo is None:
        return now.replace(tzinfo=AC_TRANSIT_TIMEZONE)
    return now.astimezone(AC_TRANSIT_TIMEZONE)


def _minutes_until_departure(value: str) -> int:
    minutes = int((_parse_departure(value) - _ac_transit_now()).total_seconds() // 60)
    return max(minutes, 1)


def _distance_meters(point_a: dict, point_b: dict) -> float:
    lat_delta = (point_a["lat"] - point_b["lat"]) * METERS_PER_DEGREE_LAT
    lng_delta = (point_a["lng"] - point_b["lng"]) * METERS_PER_DEGREE_LAT
    return (lat_delta ** 2 + lng_delta ** 2) ** 0.5

def _decode_polyline(encoded: str) -> List[List[float]]:
    coordinates: List[List[float]] = []
    index = lat = lng = 0

    while index < len(encoded):
        result = shift = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1

        result = shift = 0
        while True:
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        lng += ~(result >> 1) if result & 1 else result >> 1
        coordinates.append([lng / 1e5, lat / 1e5])

    return coordinates


def _validate_lat_lng(lat: float, lng: float) -> None:
    if not math.isfinite(lat) or not math.isfinite(lng):
        raise HTTPException(status_code=400, detail="Latitude and longitude must be finite numbers")
    if lat < -90 or lat > 90:
        raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
    if lng < -180 or lng > 180:
        raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
