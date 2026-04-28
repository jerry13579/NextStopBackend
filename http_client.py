from datetime import datetime, timezone
from typing import Any, Optional

import requests

from config import GOOGLE_MAPS_API, HTTP_TIMEOUT_SECONDS


_GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_GOOGLE_WALK_FIELD_MASK = (
    "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,"
    "routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration,"
    "routes.legs.steps.polyline.encodedPolyline,"
    "routes.legs.steps.navigationInstruction.instructions,"
    "routes.legs.steps.localizedValues.distance"
)
_GOOGLE_TRANSIT_FIELD_MASK = (
    "routes.legs.steps.travelMode,routes.legs.steps.distanceMeters,"
    "routes.legs.steps.staticDuration,routes.legs.steps.polyline.encodedPolyline,"
    "routes.legs.steps.navigationInstruction.instructions,"
    "routes.legs.steps.transitDetails.stopDetails,"
    "routes.legs.steps.transitDetails.headsign,"
    "routes.legs.steps.transitDetails.stopCount,"
    "routes.legs.steps.transitDetails.tripShortText,"
    "routes.legs.steps.transitDetails.transitLine.name,"
    "routes.legs.steps.transitDetails.transitLine.nameShort,"
    "routes.legs.steps.transitDetails.transitLine.vehicle.type"
)

def _request_json(url: str, params: Optional[dict] = None) -> Any:
    response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, payload: dict, headers: Optional[dict] = None) -> Any:
    response = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _google_routes_payload(
    start: dict,
    end: dict,
    *,
    travel_mode: str,
    departure_time: Optional[datetime] = None,
    compute_alternatives: bool = False,
) -> dict:
    payload: dict = {
        "origin": {"location": {"latLng": {"latitude": start["lat"], "longitude": start["lng"]}}},
        "destination": {"location": {"latLng": {"latitude": end["lat"], "longitude": end["lng"]}}},
        "travelMode": travel_mode,
        "polylineQuality": "HIGH_QUALITY",
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    if compute_alternatives:
        payload["computeAlternativeRoutes"] = True
    if departure_time is not None:
        depart_at = departure_time.astimezone(timezone.utc)
        payload["departureTime"] = depart_at.isoformat().replace("+00:00", "Z")
    return payload


def _google_routes_headers(field_mask: str) -> dict:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API,
        "X-Goog-FieldMask": field_mask,
    }
