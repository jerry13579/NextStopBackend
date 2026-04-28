from datetime import datetime, timezone
from typing import List, Optional

import requests
from fastapi import HTTPException

from config import GOOGLE_MAPS_API, GOOGLE_PLACES_SEARCH_RADIUS_METERS
from common import _decode_polyline, _get_any, _get_stripped
from gtfs_service import _nearest_gtfs_stop, _resolve_route_ids
from http_client import (
    _GOOGLE_ROUTES_URL,
    _GOOGLE_TRANSIT_FIELD_MASK,
    _GOOGLE_WALK_FIELD_MASK,
    _google_routes_headers,
    _google_routes_payload,
    _post_json,
    _request_json,
)

def _duration_to_minutes(value: Optional[str], fallback_minutes: int = 1) -> int:
    if not value:
        return fallback_minutes
    try:
        seconds = float(str(value).rstrip("s"))
    except ValueError:
        return fallback_minutes
    return max(round(seconds / 60), 1)


def _steps_from_google_route(steps: List[dict]) -> List[dict]:
    parsed_steps: List[dict] = []
    for step in steps:
        instruction = (
            step.get("navigationInstruction", {}).get("instructions")
            or step.get("localizedValues", {}).get("distance", {}).get("text")
            or "Continue"
        )
        parsed_steps.append(
            {
                "instruction": instruction,
                "distance_meters": step.get("distanceMeters"),
                "duration_minutes": _duration_to_minutes(step.get("staticDuration")),
                "geometry": (
                    {
                        "type": "LineString",
                        "coordinates": _decode_polyline(step["polyline"]["encodedPolyline"]),
                    }
                    if step.get("polyline", {}).get("encodedPolyline")
                    else None
                ),
            }
        )
    return parsed_steps

def _google_route(start: dict, end: dict, travel_mode: str = "WALK") -> dict:
    if not GOOGLE_MAPS_API:
        raise HTTPException(status_code=503, detail="Google Maps API key is not configured")

    payload = _google_routes_payload(start, end, travel_mode=travel_mode)
    headers = _google_routes_headers(_GOOGLE_WALK_FIELD_MASK)
    try:
        data = _post_json(_GOOGLE_ROUTES_URL, payload, headers)
        route = data["routes"][0]
        duration = route["duration"]
        duration_seconds = int(duration.rstrip("s")) if isinstance(duration, str) else int(duration)
        encoded_polyline = route.get("polyline", {}).get("encodedPolyline")
        steps = route.get("legs", [{}])[0].get("steps", [])
        return {
            "duration_minutes": max(round(duration_seconds / 60), 1),
            "distance_km": round(route["distanceMeters"] / 1000, 2),
            "source": "google_routes",
            "geometry": {
                "type": "LineString",
                "coordinates": _decode_polyline(encoded_polyline),
            } if encoded_polyline else None,
            "steps": _steps_from_google_route(steps),
        }
    except (requests.RequestException, KeyError, IndexError, TypeError):
        raise HTTPException(status_code=503, detail="Google route planning is unavailable")


def _google_transit_candidates(start: dict, end: dict, departure_time: Optional[datetime] = None) -> List[dict]:
    if not GOOGLE_MAPS_API:
        return []

    payload = _google_routes_payload(
        start,
        end,
        travel_mode="TRANSIT",
        departure_time=departure_time or datetime.now(timezone.utc),
        compute_alternatives=True,
    )
    headers = _google_routes_headers(_GOOGLE_TRANSIT_FIELD_MASK)
    try:
        data = _post_json(_GOOGLE_ROUTES_URL, payload, headers)
    except requests.RequestException:
        return []

    candidates: List[dict] = []
    for route in data.get("routes", []):
        steps = route.get("legs", [{}])[0].get("steps", [])
        for step in steps:
            if step.get("travelMode") != "TRANSIT":
                continue
            transit_details = step.get("transitDetails") or {}
            transit_line = transit_details.get("transitLine") or {}
            vehicle_type = ((transit_line.get("vehicle") or {}).get("type") or "").upper()
            if vehicle_type and vehicle_type != "BUS":
                continue
            stop_details = transit_details.get("stopDetails") or {}
            departure_stop = (((stop_details.get("departureStop") or {}).get("location") or {}).get("latLng") or {})
            arrival_stop = (((stop_details.get("arrivalStop") or {}).get("location") or {}).get("latLng") or {})
            if not departure_stop or not arrival_stop:
                continue

            boarding_point = {
                "lat": departure_stop.get("latitude"),
                "lng": departure_stop.get("longitude"),
            }
            alighting_point = {
                "lat": arrival_stop.get("latitude"),
                "lng": arrival_stop.get("longitude"),
            }
            if boarding_point["lat"] is None or boarding_point["lng"] is None:
                continue
            if alighting_point["lat"] is None or alighting_point["lng"] is None:
                continue

            boarding_stop = _nearest_gtfs_stop(
                boarding_point,
                preferred_name=(stop_details.get("departureStop") or {}).get("name"),
            )
            alighting_stop = _nearest_gtfs_stop(
                alighting_point,
                preferred_name=(stop_details.get("arrivalStop") or {}).get("name"),
            )
            route_short_name = _get_any(transit_line, "nameShort", "name") or ""
            route_ids = _resolve_route_ids(route_short_name)
            segment_geometry = (
                {
                    "type": "LineString",
                    "coordinates": _decode_polyline(step["polyline"]["encodedPolyline"]),
                }
                if step.get("polyline", {}).get("encodedPolyline")
                else None
            )
            candidates.append(
                {
                    "route_id": route_ids[0] if route_ids else None,
                    "route_short_name": route_short_name or None,
                    "trip_headsign": _get_stripped(transit_details, "headsign"),
                    "boarding_stop_id": boarding_stop["stop_id"] if boarding_stop else None,
                    "boarding_stop_name": boarding_stop["stop_name"] if boarding_stop else (stop_details.get("departureStop") or {}).get("name"),
                    "boarding_stop_point": boarding_point,
                    "alighting_stop_id": alighting_stop["stop_id"] if alighting_stop else None,
                    "alighting_stop_name": alighting_stop["stop_name"] if alighting_stop else (stop_details.get("arrivalStop") or {}).get("name"),
                    "alighting_stop_point": alighting_point,
                    "departure_time": stop_details.get("departureTime"),
                    "arrival_time": stop_details.get("arrivalTime"),
                    "segment_geometry": segment_geometry,
                    "ride_instruction": step.get("navigationInstruction", {}).get("instructions") or None,
                    "ride_distance_meters": step.get("distanceMeters"),
                    "ride_time_minutes": _duration_to_minutes(step.get("staticDuration")),
                    "stop_count": transit_details.get("stopCount"),
                }
            )
    return candidates

def _places_location_bias(proximity: Optional[dict]) -> Optional[dict]:
    if not proximity:
        return None
    return {
        "circle": {
            "center": {"latitude": proximity["lat"], "longitude": proximity["lng"]},
            "radius": float(GOOGLE_PLACES_SEARCH_RADIUS_METERS),
        }
    }


def _geocode_destination(destination: str, proximity: Optional[dict] = None) -> dict:
    if not GOOGLE_MAPS_API:
        raise HTTPException(status_code=503, detail="Google Maps API key is not configured")

    payload: dict = {"textQuery": destination, "maxResultCount": 1}
    bias = _places_location_bias(proximity)
    if bias:
        payload["locationBias"] = bias
    headers = _google_routes_headers("places.location")

    try:
        data = _post_json("https://places.googleapis.com/v1/places:searchText", payload, headers)
        location = data["places"][0]["location"]
        return {"lat": location["latitude"], "lng": location["longitude"]}
    except (requests.RequestException, KeyError, IndexError, TypeError):
        legacy_params: dict = {"query": destination, "key": GOOGLE_MAPS_API}
        if proximity:
            legacy_params["location"] = f'{proximity["lat"]},{proximity["lng"]}'
            legacy_params["radius"] = GOOGLE_PLACES_SEARCH_RADIUS_METERS
        try:
            data = _request_json("https://maps.googleapis.com/maps/api/place/textsearch/json", legacy_params)
            location = data["results"][0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}
        except (requests.RequestException, KeyError, IndexError, TypeError):
            raise HTTPException(status_code=503, detail="Destination geocoding is unavailable")
