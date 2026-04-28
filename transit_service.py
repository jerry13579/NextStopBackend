from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from common import (
    _ac_transit_now,
    _distance_meters,
    _get_any,
    _get_stripped,
    _minutes_until_departure,
    _parse_departure,
    _route_label,
    _time_to_seconds,
    _try_int,
)
from config import (
    ACTRANSIT_API_KEY,
    ETA_THRESHOLD_MINUTES,
    HTTP_TIMEOUT_SECONDS,
    NEARBY_STOPS_RADIUS_FEET,
    ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP,
    ROUTE_PLAN_MAX_STOPS,
    ROUTE_PLAN_MAX_TOTAL_OPTIONS,
    WALK_SPEED_METERS_PER_MINUTE,
)
from google_service import _google_route, _google_transit_candidates
from gtfs_service import (
    _active_gtfs_service_ids,
    _build_transit_segment,
    _gtfs_route_shape_coordinates,
    _gtfs_trip_directions,
    _headsign_is_compatible,
    _load_gtfs_routes_by_id,
    _load_gtfs_stop_times_by_stop,
    _load_gtfs_trips_by_id,
    _nearest_gtfs_stop,
    _nearest_gtfs_stop_id,
    _resolve_route_ids,
    _segment_moves_toward_destination,
)
from http_client import _request_json
from telemetry_service import _capacity_for_prediction, occupancy_seats_left

@dataclass(frozen=True)
class RouteIntent:
    source: str
    route_ids: Tuple[str, ...]
    route_short_name: Optional[str]
    boarding_stop_id: Optional[str]
    alighting_stop_id: Optional[str]
    trip_headsign: Optional[str]
    departure_time: Optional[str]
    direction_id: Optional[int]
    google_candidate: Optional[dict] = None


def _fetch_service_alert(routes: Optional[List[str]] = None) -> Optional[dict]:
    if not ACTRANSIT_API_KEY:
        return None

    route_list = [stripped for route in (routes or []) if route and (stripped := route.strip())]
    if not route_list:
        route_list = ["51B", "F", "6"]

    try:
        data = _request_json(
            "https://api.actransit.org/transit/actrealtime/servicebulletin",
            {
                "token": ACTRANSIT_API_KEY,
                "rt": ",".join(route_list[:5]),
            },
        )
    except requests.RequestException:
        return None

    requested_routes = {route.upper() for route in route_list}
    generic_alerts = {"local passengers allowed"}
    bulletins = ((data or {}).get("bustime-response") or {}).get("sb") or []

    for bulletin in bulletins:
        brief = _get_any(bulletin, "brf", "sbj", "dtl")
        if not brief or brief.lower() in generic_alerts:
            continue

        services = bulletin.get("srvc") or []
        affected_routes = sorted({
            stripped
            for service in services
            if (stripped := str(service.get("rt") or "").strip())
        })
        if not affected_routes:
            continue
        if requested_routes and not requested_routes.intersection({route.upper() for route in affected_routes}):
            continue

        first_service = services[0] if services else {}
        return {
            "summary": brief,
            "subject": _get_stripped(bulletin, "sbj"),
            "effective": _get_stripped(bulletin, "efct"),
            "route": affected_routes[0],
            "routes": affected_routes,
            "direction": _get_stripped(first_service, "rtdir"),
            "stop_name": _get_stripped(first_service, "stpnm"),
        }

    return None

def _fetch_actrealtime_vehicles(route_name: str) -> List[dict]:
    if not ACTRANSIT_API_KEY:
        return []
    try:
        data = _request_json(
            "https://api.actransit.org/transit/actrealtime/vehicle",
            {"rt": route_name, "tmres": "s", "token": ACTRANSIT_API_KEY},
        )
    except requests.RequestException:
        return []
    response = (data or {}).get("bustime-response") if isinstance(data, dict) else None
    vehicles = (response or {}).get("vehicle") or []
    return vehicles if isinstance(vehicles, list) else []


def _fetch_legacy_route_vehicles(route_name: str) -> List[dict]:
    if not ACTRANSIT_API_KEY:
        return []
    try:
        data = _request_json(
            f"https://api.actransit.org/transit/route/{route_name}/vehicles",
            {"token": ACTRANSIT_API_KEY},
        )
    except requests.RequestException:
        return []
    return data if isinstance(data, list) else []


def _parse_actrealtime_timestamp(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # Format from BusTime: "20260428 14:43:43" or "20260428 14:43"
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw


def _normalize_actrealtime_vehicle(entry: dict) -> Optional[dict]:
    vid = entry.get("vid")
    if vid is None:
        return None
    try:
        lat = float(entry.get("lat"))
        lng = float(entry.get("lon"))
    except (TypeError, ValueError):
        return None
    try:
        heading = int(entry.get("hdg")) if entry.get("hdg") is not None else None
    except (TypeError, ValueError):
        heading = None
    static_trip_id = entry.get("tripid")
    return {
        "VehicleId": _try_int(vid) if str(vid).isdigit() else vid,
        "CurrentTripId": _try_int(static_trip_id) if static_trip_id is not None else None,
        "Latitude": lat,
        "Longitude": lng,
        "Heading": heading,
        "TimeLastReported": _parse_actrealtime_timestamp(entry.get("tmstmp")),
        "psgld": entry.get("psgld") or None,
        "destination": entry.get("des"),
        "tatripid": entry.get("tatripid"),
        "delayed": bool(entry.get("dly")),
        "pattern_id": entry.get("pid"),
    }


def _fetch_route_vehicles(route_name: str, direction_id: Optional[int] = None) -> List[dict]:
    raw_vehicles = _fetch_actrealtime_vehicles(route_name)
    normalized: List[dict] = []
    for entry in raw_vehicles:
        vehicle = _normalize_actrealtime_vehicle(entry)
        if vehicle is not None:
            normalized.append(vehicle)

    if not normalized:
        # BusTime endpoint silent — fall back to legacy feed so we never go to zero.
        return _fetch_legacy_route_vehicles(route_name)

    if direction_id is None:
        return normalized

    try:
        trip_directions = _gtfs_trip_directions()
    except HTTPException:
        # Without GTFS we can't verify direction; return everything rather than zero.
        return normalized

    filtered: List[dict] = []
    for vehicle in normalized:
        trip_id = vehicle.get("CurrentTripId")
        trip_direction = trip_directions.get(str(trip_id)) if trip_id is not None else None
        if trip_direction is None or trip_direction == direction_id:
            filtered.append(vehicle)
    return filtered


def _fetch_trip_pattern(route_name: str, trip_id: Optional[int]) -> Optional[dict]:
    try:
        gtfs_coordinates = _gtfs_route_shape_coordinates(route_name, direction_id=None)
        if len(gtfs_coordinates) >= 2:
            return {
                "type": "LineString",
                "coordinates": [[point["lng"], point["lat"]] for point in gtfs_coordinates],
                "source": "gtfs_static",
            }
    except HTTPException as exc:
        print(f"[trip_pattern] GTFS shape lookup failed for route {route_name}: {exc!r}")

    if not ACTRANSIT_API_KEY or not trip_id:
        return None

    try:
        data = _request_json(
            f"https://api.actransit.org/transit/route/{route_name}/trip/{trip_id}/pattern",
            {"token": ACTRANSIT_API_KEY},
        )
    except requests.RequestException:
        return None

    points = data if isinstance(data, list) else data.get("Points") or data.get("points") or []
    coordinates = []
    for point in points:
        lat = point.get("Latitude") or point.get("latitude") or point.get("Lat")
        lng = point.get("Longitude") or point.get("longitude") or point.get("Lon") or point.get("Lng")
        if lat is None or lng is None:
            continue
        try:
            coordinates.append([float(lng), float(lat)])
        except (TypeError, ValueError):
            continue

    if len(coordinates) < 2:
        return None
    return {"type": "LineString", "coordinates": coordinates}

def _unique_strings(values: List[Optional[str]]) -> Tuple[str, ...]:
    unique: List[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = str(value or "").strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        unique.append(stripped)
    return tuple(unique)


def _safe_resolve_route_ids(route_identifier: Optional[str]) -> Tuple[str, ...]:
    route_key = str(route_identifier or "").strip()
    if not route_key:
        return tuple()
    try:
        return tuple(_resolve_route_ids(route_key))
    except HTTPException:
        return tuple()


def _prediction_headsign(prediction: dict) -> Optional[str]:
    return _get_any(prediction, "Destination", "DirectionText")


def _prediction_route_ids(prediction: dict) -> Tuple[str, ...]:
    return _safe_resolve_route_ids(_get_stripped(prediction, "RouteName"))


def _google_candidate_route_ids(candidate: dict) -> Tuple[str, ...]:
    route_id = _get_stripped(candidate, "route_id")
    route_short_name = _get_stripped(candidate, "route_short_name")
    return _unique_strings([
        route_id,
        *_safe_resolve_route_ids(route_id),
        *_safe_resolve_route_ids(route_short_name),
    ])


def _same_route_label(left: Optional[str], right: Optional[str]) -> bool:
    left_key = str(left or "").strip().upper()
    right_key = str(right or "").strip().upper()
    return bool(left_key and right_key and left_key == right_key)


def _parse_optional_departure(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _parse_departure(value)
    except ValueError:
        return None


def _departure_delta_minutes(left: Optional[str], right: Optional[str]) -> Optional[float]:
    left_time = _parse_optional_departure(left)
    right_time = _parse_optional_departure(right)
    if left_time is None or right_time is None:
        return None
    return abs((left_time - right_time).total_seconds()) / 60


def _prediction_source(prediction: dict) -> str:
    return "gtfs_schedule" if _get_stripped(prediction, "Source") == "schedule" else "live_prediction"


def _candidate_matches_boarding_stop(
    candidate: dict,
    stop: Optional[dict],
    boarding_stop_id: Optional[str],
) -> bool:
    candidate_stop_id = str(candidate.get("boarding_stop_id") or "").strip()
    known_stop_ids = {
        str(value or "").strip()
        for value in [
            boarding_stop_id,
            (stop or {}).get("stop_id"),
        ]
        if str(value or "").strip()
    }
    if candidate_stop_id and candidate_stop_id in known_stop_ids:
        return True

    boarding_point = candidate.get("boarding_stop_point")
    if not stop or not boarding_point:
        return False
    try:
        return _distance_meters(stop, boarding_point) <= 160
    except (KeyError, TypeError, ValueError):
        return False


def _route_intent_from_google_candidate(
    candidate: dict,
    prediction: dict,
    boarding_stop_id: Optional[str],
) -> RouteIntent:
    route_short_name = _get_stripped(candidate, "route_short_name") or _get_stripped(prediction, "RouteName")
    return RouteIntent(
        source="google_transit",
        route_ids=_google_candidate_route_ids(candidate),
        route_short_name=route_short_name,
        boarding_stop_id=str(candidate.get("boarding_stop_id") or boarding_stop_id or "").strip() or None,
        alighting_stop_id=str(candidate.get("alighting_stop_id") or "").strip() or None,
        trip_headsign=_get_stripped(candidate, "trip_headsign") or _prediction_headsign(prediction),
        departure_time=candidate.get("departure_time") or prediction.get("PredictedDeparture"),
        direction_id=_try_int(prediction.get("DirectionId")),
        google_candidate=candidate,
    )


def _route_intent_from_prediction(
    prediction: dict,
    boarding_stop_id: Optional[str],
    destination_stop_id: Optional[str],
) -> Optional[RouteIntent]:
    route_name = _get_stripped(prediction, "RouteName")
    route_ids = _prediction_route_ids(prediction)
    if not route_name and not route_ids:
        return None
    return RouteIntent(
        source=_prediction_source(prediction),
        route_ids=route_ids,
        route_short_name=route_name,
        boarding_stop_id=str(boarding_stop_id or "").strip() or None,
        alighting_stop_id=str(destination_stop_id or "").strip() or None,
        trip_headsign=_prediction_headsign(prediction),
        departure_time=prediction.get("PredictedDeparture"),
        direction_id=_try_int(prediction.get("DirectionId")),
    )


def _prediction_matches_route_intent(prediction: dict, intent: RouteIntent) -> bool:
    route_name = _get_stripped(prediction, "RouteName")
    prediction_route_ids = set(_prediction_route_ids(prediction))
    intent_route_ids = set(intent.route_ids)
    if intent_route_ids:
        route_id_match = bool(prediction_route_ids and prediction_route_ids.intersection(intent_route_ids))
        route_label_match = _same_route_label(route_name, intent.route_short_name)
        if not route_id_match and not route_label_match:
            return False
    elif intent.route_short_name and not _same_route_label(route_name, intent.route_short_name):
        return False

    prediction_direction_id = _try_int(prediction.get("DirectionId"))
    if intent.direction_id is not None and prediction_direction_id != intent.direction_id:
        return False

    prediction_headsign = _prediction_headsign(prediction)
    if (
        intent.direction_id is not None
        and intent.trip_headsign
        and prediction_headsign
        and not _headsign_is_compatible(prediction_headsign, intent.trip_headsign)
    ):
        return False
    return True


def _route_intent_score_for_prediction(prediction: dict, intent: RouteIntent) -> float:
    score = 0.0
    route_name = _get_stripped(prediction, "RouteName")
    prediction_route_ids = set(_prediction_route_ids(prediction))
    intent_route_ids = set(intent.route_ids)
    if intent_route_ids and prediction_route_ids.intersection(intent_route_ids):
        score += 100
    if _same_route_label(route_name, intent.route_short_name):
        score += 80
    prediction_direction_id = _try_int(prediction.get("DirectionId"))
    if intent.direction_id is not None and prediction_direction_id == intent.direction_id:
        score += 50
    if intent.trip_headsign and _headsign_is_compatible(_prediction_headsign(prediction), intent.trip_headsign):
        score += 20
    delta_minutes = _departure_delta_minutes(prediction.get("PredictedDeparture"), intent.departure_time)
    if delta_minutes is not None:
        score += max(0, 20 - min(delta_minutes, 20))
    if intent.source == "google_transit":
        score += 15
    return score


def _select_route_intent(
    prediction: dict,
    *,
    stop: Optional[dict],
    boarding_stop_id: Optional[str],
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
) -> Optional[RouteIntent]:
    google_intents: List[RouteIntent] = []
    for candidate in google_candidates:
        if not _candidate_matches_boarding_stop(candidate, stop, boarding_stop_id):
            continue
        intent = _route_intent_from_google_candidate(candidate, prediction, boarding_stop_id)
        if _prediction_matches_route_intent(prediction, intent):
            google_intents.append(intent)

    if google_intents:
        return max(
            google_intents,
            key=lambda intent: _route_intent_score_for_prediction(prediction, intent),
        )
    return _route_intent_from_prediction(prediction, boarding_stop_id, destination_stop_id)


def _select_route_planning_predictions(
    predictions: List[dict],
    stop: Optional[dict] = None,
    destination_stop_id: Optional[str] = None,
    google_candidates: Optional[List[dict]] = None,
    limit: int = ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP,
) -> List[dict]:
    selected: List[dict] = []
    seen: set[tuple] = set()

    def sort_key(prediction: dict) -> tuple[float, datetime]:
        intent = _select_route_intent(
            prediction,
            stop=stop,
            boarding_stop_id=(stop or {}).get("stop_id"),
            destination_stop_id=destination_stop_id,
            google_candidates=google_candidates or [],
        )
        score = _route_intent_score_for_prediction(prediction, intent) if intent else 0
        try:
            departure = _parse_departure(prediction["PredictedDeparture"])
        except (KeyError, ValueError):
            departure = datetime.max.replace(tzinfo=timezone.utc)
        return (-score, departure)

    for prediction in sorted(predictions, key=sort_key):
        try:
            eta = _minutes_until_departure(prediction["PredictedDeparture"])
        except (KeyError, ValueError):
            continue
        if eta > ETA_THRESHOLD_MINUTES:
            continue
        intent = _select_route_intent(
            prediction,
            stop=stop,
            boarding_stop_id=(stop or {}).get("stop_id"),
            destination_stop_id=destination_stop_id,
            google_candidates=google_candidates or [],
        )
        if intent is None or not _prediction_matches_route_intent(prediction, intent):
            continue
        key = (
            str(prediction.get("RouteName") or "").strip(),
            str(prediction.get("Destination") or prediction.get("DirectionText") or "").strip(),
            str(prediction.get("DirectionId") or "").strip(),
            str(prediction.get("TripId") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(prediction)
        if len(selected) >= limit:
            break
    return selected

def _fetch_nearby_stops(
    latitude: float,
    longitude: float,
    distance_feet: int = NEARBY_STOPS_RADIUS_FEET,
) -> tuple[List[dict], str]:
    if not ACTRANSIT_API_KEY:
        return [], "unavailable"

    try:
        data = _request_json(
            f"https://api.actransit.org/transit/stops/{latitude}/{longitude}/{distance_feet}/true/",
            {
                "token": ACTRANSIT_API_KEY,
            },
        )
        return data or [], "live"
    except requests.RequestException:
        return [], "unavailable"


def _normalize_stop(stop: dict) -> dict:
    return {
        "stop_id": str(stop.get("StopId")),
        "name": stop.get("Name", "Nearby AC Transit stop"),
        "lat": float(stop.get("Latitude")),
        "lng": float(stop.get("Longitude")),
        "city": stop.get("City"),
    }


def _estimated_walk_route(start: dict, end: dict) -> dict:
    distance_meters = _distance_meters(start, end)
    duration_minutes = max(round(distance_meters / WALK_SPEED_METERS_PER_MINUTE), 1)
    return {
        "duration_minutes": duration_minutes,
        "distance_km": round(distance_meters / 1000, 2),
        "source": "estimated_walk",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [start["lng"], start["lat"]],
                [end["lng"], end["lat"]],
            ],
        },
        "steps": [
            {
                "instruction": "Walk to destination",
                "distance_meters": round(distance_meters),
                "duration_minutes": duration_minutes,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [start["lng"], start["lat"]],
                        [end["lng"], end["lat"]],
                    ],
                },
            }
        ],
    }


def _safe_walk_route(start: dict, end: dict) -> dict:
    try:
        return _google_route(start, end)
    except HTTPException as exc:
        print(f"[plan_trip] Google walking route unavailable; using estimate: {exc!r}")
        return _estimated_walk_route(start, end)


def _prediction_is_upcoming(prediction: dict, now: Optional[datetime] = None) -> bool:
    departure_value = prediction.get("PredictedDeparture")
    if not departure_value:
        return False
    try:
        departure = _parse_departure(departure_value)
    except ValueError:
        return False
    comparison_now = _ac_transit_now(now)
    return departure > comparison_now


def _scheduled_predictions_for_stop(stop: dict, now: Optional[datetime] = None) -> List[dict]:
    gtfs_stop = _nearest_gtfs_stop(
        {"lat": stop["lat"], "lng": stop["lng"]},
        preferred_name=stop.get("name"),
    )
    if not gtfs_stop:
        return []

    local_now = _ac_transit_now(now)
    now_seconds = _time_to_seconds(local_now.hour, local_now.minute, local_now.second)
    active_service_ids = _active_gtfs_service_ids(local_now)
    if not active_service_ids:
        return []

    trips_by_id = _load_gtfs_trips_by_id()
    routes_by_id = _load_gtfs_routes_by_id()
    stop_times = _load_gtfs_stop_times_by_stop().get(gtfs_stop["stop_id"], [])
    predictions: List[dict] = []
    for stop_time in stop_times:
        departure_seconds = stop_time.get("departure_seconds")
        if departure_seconds is None or departure_seconds <= now_seconds:
            continue
        trip = trips_by_id.get(stop_time["trip_id"])
        if not trip:
            continue
        service_id = _get_stripped(trip, "service_id")
        if not service_id or service_id not in active_service_ids:
            continue
        route_id = _get_stripped(trip, "route_id") or ""
        route = routes_by_id.get(route_id, {})
        route_name = _route_label(route, route_id)
        departure_time = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=local_now.tzinfo) + timedelta(seconds=departure_seconds)
        predictions.append(
            {
                "RouteName": route_name,
                "PredictedDeparture": departure_time.isoformat(),
                "DirectionId": trip.get("direction_id"),
                "DirectionText": trip.get("trip_headsign"),
                "Destination": trip.get("trip_headsign"),
                "TripId": trip.get("trip_id"),
                "VehicleId": None,
                "PredictionDateTime": local_now.isoformat(),
                "Source": "schedule",
            }
        )
    return predictions[:12]


def _active_service_ids_for_prediction(prediction: dict) -> set[str]:
    departure = _parse_optional_departure(prediction.get("PredictedDeparture"))
    return _active_gtfs_service_ids(departure)


def _segment_matches_route_intent(segment: dict, intent: RouteIntent, destination_point: dict) -> bool:
    segment_route_id = _get_stripped(segment, "route_id")
    segment_route_short_name = _get_stripped(segment, "route_short_name")
    if intent.route_ids and segment_route_id not in set(intent.route_ids):
        return False
    if (
        not intent.route_ids
        and intent.route_short_name
        and not _same_route_label(segment_route_short_name, intent.route_short_name)
    ):
        return False

    segment_direction_id = _try_int(segment.get("direction_id"))
    if intent.direction_id is not None and segment_direction_id != intent.direction_id:
        return False

    boarding_stop_id = str((segment.get("boarding_stop") or {}).get("stop_id") or "").strip()
    if intent.boarding_stop_id and boarding_stop_id != intent.boarding_stop_id:
        return False

    alighting_stop_id = str((segment.get("alighting_stop") or {}).get("stop_id") or "").strip()
    if intent.google_candidate and intent.alighting_stop_id and alighting_stop_id != intent.alighting_stop_id:
        return False

    if (
        intent.direction_id is not None
        and intent.trip_headsign
        and not _headsign_is_compatible(segment.get("trip_headsign"), intent.trip_headsign)
    ):
        return False
    return _segment_moves_toward_destination(segment, destination_point)


def _direction_match_source(intent: RouteIntent) -> str:
    if intent.direction_id is not None:
        return "gtfs_direction"
    if intent.google_candidate:
        return "google_stops"
    return "segment_geometry"


def _build_one_available_bus(
    prediction: dict,
    *,
    index: int,
    stop: dict,
    boarding_stop_id: str,
    destination_marker: dict,
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
    walk_to_stop: dict,
) -> Optional[dict]:
    route_name = _get_stripped(prediction, "RouteName") or f"Route {index + 1}"
    delay_seconds = int(prediction.get("PredictedDelayInSeconds", 0) or 0)
    delay_minutes = round(delay_seconds / 60)
    eta = _minutes_until_departure(prediction["PredictedDeparture"])
    if eta > ETA_THRESHOLD_MINUTES:
        return None
    prediction_direction_id = _try_int(prediction.get("DirectionId"))
    route_intent = _select_route_intent(
        prediction,
        stop=stop,
        boarding_stop_id=boarding_stop_id,
        destination_stop_id=destination_stop_id,
        google_candidates=google_candidates,
    )
    if route_intent is None or not _prediction_matches_route_intent(prediction, route_intent):
        return None
    google_candidate = route_intent.google_candidate
    try:
        transit_segment = _build_transit_segment(
            route_name=route_name,
            trip_id=prediction.get("TripId"),
            prediction_direction_id=prediction_direction_id,
            boarding_stop_id=boarding_stop_id,
            destination_stop_id=destination_stop_id,
            destination_point=destination_marker,
            google_candidate=google_candidate,
            active_service_ids=_active_service_ids_for_prediction(prediction),
        )
    except HTTPException:
        return None
    if not transit_segment:
        return None
    if not _segment_matches_route_intent(transit_segment, route_intent, destination_marker):
        return None

    alighting_stop = transit_segment.get("alighting_stop") or {}
    walk_from_origin = (
        {"lat": alighting_stop["lat"], "lng": alighting_stop["lng"]}
        if alighting_stop
        else {"lat": stop["lat"], "lng": stop["lng"]}
    )
    walk_from_stop = _safe_walk_route(walk_from_origin, destination_marker)

    trip_headsign = transit_segment.get("trip_headsign") or _get_any(prediction, "Destination", "DirectionText")
    prediction_headsign = _get_any(prediction, "Destination", "DirectionText")
    if (
        route_intent.direction_id is not None
        and prediction_headsign
        and not _headsign_is_compatible(trip_headsign, prediction_headsign)
    ):
        return None

    capacity, occupancy = _capacity_for_prediction(prediction, route_name)
    seats_left = occupancy_seats_left(capacity)
    bus_position = None
    if occupancy and occupancy.get("last_position_lat") is not None:
        bus_position = {
            "lat": occupancy["last_position_lat"],
            "lng": occupancy["last_position_lng"],
        }
    fullness = capacity if capacity is not None else (occupancy.get("estimated_occupancy_percentage") if occupancy else None)

    ride_time_minutes = int(transit_segment.get("ride_time_minutes") or 0)
    wait_time_minutes = eta
    total_trip_time_minutes = (
        walk_to_stop["duration_minutes"] + wait_time_minutes
        + ride_time_minutes + walk_from_stop["duration_minutes"]
    )
    trip_id_from_segment = _try_int(transit_segment.get("trip_id"))

    return {
        "id": f"{route_name}-{prediction.get('TripId', index)}-{stop['stop_id']}",
        "route_number": route_name,
        "route_name": transit_segment.get("route_long_name") or f"{route_name} service",
        "current_stop": stop["name"],
        "next_stop": alighting_stop["stop_name"] if alighting_stop else "Destination stop",
        "eta": eta,
        "capacity": capacity,
        "delay": delay_minutes,
        "distance": walk_to_stop["distance_km"],
        "walk_time": walk_to_stop["duration_minutes"],
        "destination_walk_time": walk_from_stop["duration_minutes"],
        "wait_time_minutes": wait_time_minutes,
        "ride_time_minutes": ride_time_minutes,
        "total_trip_time_minutes": total_trip_time_minutes,
        "ride_distance_meters": (google_candidate or {}).get("ride_distance_meters"),
        "stop_count": (google_candidate or {}).get("stop_count"),
        "seats_left": seats_left,
        "recommendation": (
            f"Walk {walk_to_stop['duration_minutes']} min to {stop['name']} for Route {route_name} toward {trip_headsign}."
            if capacity is None or capacity < 90
            else f"Route {route_name} has reported crowding. Consider another prediction or walking."
        ),
        "vehicle_id": prediction.get("VehicleId"),
        "prediction_trip_id": prediction.get("TripId"),
        "trip_id": trip_id_from_segment if trip_id_from_segment is not None else prediction.get("TripId"),
        "direction_id": transit_segment.get("direction_id") if transit_segment else prediction_direction_id,
        "route_id": transit_segment.get("route_id"),
        "route_short_name": transit_segment.get("route_short_name") or route_name,
        "shape_id": transit_segment.get("shape_id"),
        "trip_headsign": trip_headsign,
        "departure_time": prediction.get("PredictedDeparture"),
        "boarding_stop": transit_segment.get("boarding_stop"),
        "alighting_stop": transit_segment.get("alighting_stop"),
        "boarding_before_alighting": transit_segment.get("boarding_before_alighting"),
        "segment_stops": transit_segment.get("segment_stops") or [],
        "prediction_time": prediction.get("PredictionDateTime"),
        "occupancy": occupancy,
        "occupancy_percent": fullness,
        "bus_position": bus_position,
        "route_geometry": transit_segment.get("geometry"),
        "full_route_geometry": transit_segment.get("full_route_geometry"),
        "geometry_source": transit_segment.get("geometry_source"),
        "walk_to_stop": walk_to_stop,
        "walk_from_stop": walk_from_stop,
        "ride_instruction": (google_candidate or {}).get("ride_instruction"),
        "match_source": route_intent.source,
        "direction_match_source": _direction_match_source(route_intent),
    }


def _build_available_buses(
    predictions: List[dict],
    user_position: dict,
    stop: dict,
    destination_marker: dict,
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
) -> List[dict]:
    walk_to_stop = _safe_walk_route(user_position, {"lat": stop["lat"], "lng": stop["lng"]})
    matched_boarding_stop = _nearest_gtfs_stop(
        {"lat": stop["lat"], "lng": stop["lng"]},
        preferred_name=stop["name"],
    )
    boarding_stop_id = matched_boarding_stop["stop_id"] if matched_boarding_stop else stop["stop_id"]

    sorted_predictions = sorted(
        predictions,
        key=lambda item: _parse_departure(item["PredictedDeparture"]),
    )
    buses: List[dict] = []
    for index, prediction in enumerate(sorted_predictions):
        try:
            bus = _build_one_available_bus(
                prediction,
                index=index,
                stop=stop,
                boarding_stop_id=boarding_stop_id,
                destination_marker=destination_marker,
                destination_stop_id=destination_stop_id,
                google_candidates=google_candidates,
                walk_to_stop=walk_to_stop,
            )
        except (HTTPException, KeyError, ValueError, TypeError, requests.RequestException) as exc:
            print(f"[plan_trip] prediction route build failed: {exc!r}")
            continue
        if bus is not None:
            buses.append(bus)
    return buses

def _fetch_predictions(stop_id: str) -> tuple[List[dict], str]:
    if not ACTRANSIT_API_KEY:
        return [], "unavailable"

    url = f"https://api.actransit.org/transit/stops/{stop_id}/predictions?token={ACTRANSIT_API_KEY}"
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json() or []
        live_predictions = [p for p in data if _prediction_is_upcoming(p)]
        return live_predictions, "live"
    except (requests.RequestException, ValueError):
        return [], "unavailable"

def _collect_candidate_stops(
    start_marker: dict,
    google_candidates: List[dict],
) -> tuple[List[dict], List[dict], str]:
    """Return (normalized AC Transit nearby stops, candidate stops including Google boarding stops, stop_source)."""
    nearby_stops, stop_source = _fetch_nearby_stops(start_marker["lat"], start_marker["lng"])
    normalized_stops: List[dict] = []
    for stop in nearby_stops[:8]:
        try:
            normalized_stops.append(_normalize_stop(stop))
        except (TypeError, ValueError):
            continue

    candidate_stops = normalized_stops[:]
    seen = {stop["stop_id"] for stop in candidate_stops}
    for candidate in google_candidates:
        boarding_stop_id = candidate.get("boarding_stop_id")
        boarding_point = candidate.get("boarding_stop_point") or {}
        if not boarding_stop_id or str(boarding_stop_id) in seen:
            continue
        if boarding_point.get("lat") is None or boarding_point.get("lng") is None:
            continue
        candidate_stops.append({
            "stop_id": str(boarding_stop_id),
            "name": candidate.get("boarding_stop_name") or "Selected boarding stop",
            "lat": boarding_point["lat"],
            "lng": boarding_point["lng"],
            "city": None,
        })
        seen.add(str(boarding_stop_id))
    return normalized_stops, candidate_stops, stop_source


def _route_planning_stops(
    candidate_stops: List[dict],
    google_candidates: List[dict],
) -> List[dict]:
    """Pick up to ROUTE_PLAN_MAX_STOPS, preferring stops Google chose as boarding points."""
    google_boarding_ids = {
        str(candidate["boarding_stop_id"])
        for candidate in google_candidates
        if candidate.get("boarding_stop_id")
    }
    seen: set[str] = set()
    selected: List[dict] = []

    def add(stop: dict) -> None:
        if stop["stop_id"] in seen:
            return
        seen.add(stop["stop_id"])
        selected.append(stop)

    for stop in candidate_stops:
        if len(selected) >= ROUTE_PLAN_MAX_STOPS:
            break
        if stop["stop_id"] in google_boarding_ids:
            add(stop)
    for stop in candidate_stops:
        if len(selected) >= ROUTE_PLAN_MAX_STOPS:
            break
        add(stop)
    return selected


def _fetch_buses_for_stops(
    route_planning_stops: List[dict],
    start_marker: dict,
    destination_marker: dict,
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
) -> tuple[List[str], List[dict]]:
    prediction_sources: List[str] = []
    available_buses: List[dict] = []

    def fetch_stop_buses(stop: dict) -> tuple[str, List[dict]]:
        predictions, prediction_source = _fetch_predictions(stop["stop_id"])
        if not predictions:
            predictions = _scheduled_predictions_for_stop(stop)
            if predictions:
                prediction_source = "schedule"
        predictions = _select_route_planning_predictions(
            predictions,
            stop=stop,
            destination_stop_id=destination_stop_id,
            google_candidates=google_candidates,
        )
        buses = _build_available_buses(
            predictions,
            start_marker,
            stop,
            destination_marker,
            destination_stop_id,
            google_candidates,
        )
        return prediction_source, buses

    max_workers = max(1, min(ROUTE_PLAN_MAX_STOPS, len(route_planning_stops)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_stop_buses, stop): stop for stop in route_planning_stops}
        for future in as_completed(futures):
            stop = futures[future]
            try:
                prediction_source, buses = future.result()
                prediction_sources.append(prediction_source)
                available_buses.extend(buses)
            except (HTTPException, requests.RequestException, KeyError, ValueError, TypeError) as exc:
                print(f"[plan_trip] stop {stop.get('stop_id')} bus fetch failed: {exc!r}")
    return prediction_sources, available_buses


def _dedupe_and_rank_buses(buses: List[dict], direct_walk: Optional[dict]) -> List[dict]:
    def rank_number(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(10**9)

    def rank_key(bus: dict) -> tuple[int, float, float]:
        has_confirmed_intent = (
            bus.get("match_source") in {"google_transit", "live_prediction", "gtfs_schedule"}
            and bus.get("direction_match_source") in {"google_stops", "gtfs_direction", "segment_geometry"}
        )
        return (
            0 if has_confirmed_intent else 1,
            rank_number(bus.get("total_trip_time_minutes")),
            rank_number(bus.get("eta")),
        )

    deduped: Dict[tuple, dict] = {}
    for bus in buses:
        key = (
            bus.get("route_id") or bus.get("route_short_name") or bus.get("route_number"),
            bus.get("direction_id"),
            (bus.get("boarding_stop") or {}).get("stop_id"),
            (bus.get("alighting_stop") or {}).get("stop_id"),
        )
        existing = deduped.get(key)
        if existing is None or rank_key(bus) < rank_key(existing):
            deduped[key] = bus

    direct_walk_minutes = direct_walk.get("duration_minutes") if direct_walk else None

    def beats_walking(bus: dict) -> bool:
        if direct_walk_minutes is None:
            return True
        if bus.get("total_trip_time_minutes", 10**9) <= direct_walk_minutes:
            return True
        walk_total = int(bus.get("walk_time") or 0) + int(bus.get("destination_walk_time") or 0)
        return walk_total < direct_walk_minutes

    return sorted(
        (bus for bus in deduped.values() if beats_walking(bus)),
        key=rank_key,
    )[:ROUTE_PLAN_MAX_TOTAL_OPTIONS]


def _pick_response_source(prediction_sources: List[str]) -> str:
    return next(
        (source for source in ("live", "schedule", "unavailable") if source in prediction_sources),
        "unavailable",
    )


def build_plan_trip_response(
    *,
    destination: str,
    start: str,
    start_lat: float,
    start_lng: float,
    destination_lat: Optional[float],
    destination_lng: Optional[float],
    mode: str = "full",
    include_walk: bool = True,
) -> dict:
    from google_service import _geocode_destination

    bus_only = mode == "bus" or include_walk is False
    start_marker = {"lat": start_lat, "lng": start_lng}
    destination_marker = (
        {"lat": destination_lat, "lng": destination_lng}
        if destination_lat is not None and destination_lng is not None
        else _geocode_destination(destination, start_marker)
    )
    google_candidates = _google_transit_candidates(start_marker, destination_marker)

    normalized_stops, candidate_stops, stop_source = _collect_candidate_stops(start_marker, google_candidates)
    origin_stop = candidate_stops[0] if candidate_stops else None
    start_stop_id = origin_stop["stop_id"] if origin_stop else None
    destination_stop_id = _nearest_gtfs_stop_id(destination_marker)
    direct_walk = None if bus_only else _safe_walk_route(start_marker, destination_marker)

    selected_stops = _route_planning_stops(candidate_stops, google_candidates)
    prediction_sources, available_buses = _fetch_buses_for_stops(
        selected_stops,
        start_marker,
        destination_marker,
        destination_stop_id,
        google_candidates,
    )
    available_buses = _dedupe_and_rank_buses(available_buses, direct_walk)

    source = _pick_response_source(prediction_sources)
    next_bus = available_buses[0] if available_buses else None
    recommendation = (
        "No buses available right now"
        if next_bus is None
        else f"Walk to {next_bus['current_stop']} for Route {next_bus['route_number']} toward {next_bus.get('trip_headsign') or destination}."
    )

    response = {
        "source": source,
        "start": start,
        "destination": destination,
        "start_stop_id": start_stop_id,
        "destination_stop_id": destination_stop_id,
        "next_bus": next_bus,
        "data_sources": {
            "nearby_stops": stop_source,
            "predictions": source if source in {"live", "schedule"} else "unavailable",
            "destination_geocoding": "provided_coordinates" if destination_lat is not None and destination_lng is not None else "google_places",
            "transit_itinerary": "google_routes" if google_candidates else "gtfs_only",
        },
        "origin_stop": origin_stop,
        "nearby_stops": normalized_stops,
        "available_buses": available_buses,
        "recommendation": recommendation,
        "map_data": {
            "start_marker": start_marker,
            "destination_marker": destination_marker,
            "origin_stop_marker": {"lat": origin_stop["lat"], "lng": origin_stop["lng"]} if origin_stop else None,
        },
    }
    if not bus_only:
        response["direct_walk"] = direct_walk
    return response
