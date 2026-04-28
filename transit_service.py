from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from fastapi import HTTPException

from common import (
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
)
from http_client import _request_json
from telemetry_service import _capacity_for_prediction, occupancy_seats_left

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

def _fetch_route_vehicles(route_name: str, direction_id: Optional[int] = None) -> List[dict]:
    if not ACTRANSIT_API_KEY:
        return []

    try:
        data = _request_json(
            f"https://api.actransit.org/transit/route/{route_name}/vehicles",
            {"token": ACTRANSIT_API_KEY},
        )
    except requests.RequestException:
        return []

    vehicles = data if isinstance(data, list) else []
    if direction_id is None:
        return vehicles

    try:
        trip_directions = _gtfs_trip_directions()
    except HTTPException:
        return vehicles

    filtered: List[dict] = []
    for vehicle in vehicles:
        trip_id = vehicle.get("CurrentTripId")
        if trip_id is None:
            continue
        trip_direction = trip_directions.get(str(trip_id))
        if trip_direction is not None and trip_direction == direction_id:
            filtered.append(vehicle)
    # AC Transit realtime trip ids can differ from the static GTFS trip ids in
    # trips.txt. If none match, keep the live route vehicles visible rather than
    # hiding every bus marker.
    return filtered or vehicles


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

def _google_candidate_score_for_prediction(prediction: dict, google_candidates: List[dict]) -> int:
    if not google_candidates:
        return 0
    route_name = str(prediction.get("RouteName") or "").strip()
    route_ids = set(_resolve_route_ids(route_name))
    prediction_headsign = prediction.get("Destination") or prediction.get("DirectionText")

    def candidate_score(candidate: dict) -> int:
        score = 0
        candidate_route = _get_stripped(candidate, "route_short_name")
        candidate_route_id = _get_stripped(candidate, "route_id")
        if candidate_route and candidate_route == route_name:
            score += 20
        if candidate_route_id and candidate_route_id in route_ids:
            score += 20
        if prediction_headsign and _headsign_is_compatible(prediction_headsign, candidate.get("trip_headsign")):
            score += 10
        return score

    return max((candidate_score(candidate) for candidate in google_candidates), default=0)


def _select_route_planning_predictions(
    predictions: List[dict],
    google_candidates: Optional[List[dict]] = None,
    limit: int = ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP,
) -> List[dict]:
    selected: List[dict] = []
    seen: set[tuple] = set()

    def sort_key(prediction: dict) -> tuple[int, datetime]:
        score = _google_candidate_score_for_prediction(prediction, google_candidates or [])
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


def _active_gtfs_service_ids(now: Optional[datetime] = None) -> set[str]:
    local_now = _ac_transit_now(now)
    date_key = local_now.strftime("%Y%m%d")
    weekday_key = local_now.strftime("%A").lower()
    active_services: set[str] = set()

    for row in _load_gtfs_calendar():
        service_id = _get_stripped(row, "service_id")
        if not service_id:
            continue
        if (row.get(weekday_key) or "0").strip() != "1":
            continue
        start_date = _get_stripped(row, "start_date")
        end_date = _get_stripped(row, "end_date")
        if start_date and date_key < start_date:
            continue
        if end_date and date_key > end_date:
            continue
        active_services.add(service_id)

    for row in _load_gtfs_calendar_dates().get(date_key, []):
        service_id = _get_stripped(row, "service_id")
        if not service_id:
            continue
        exception_type = _get_stripped(row, "exception_type")
        if exception_type == "1":
            active_services.add(service_id)
        elif exception_type == "2":
            active_services.discard(service_id)

    return active_services


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


def _select_google_transit_candidate(
    route_name: str,
    stop: dict,
    prediction: dict,
    google_candidates: List[dict],
) -> Optional[dict]:
    if not google_candidates:
        return None

    route_ids = set(_resolve_route_ids(route_name))
    prediction_departure = prediction.get("PredictedDeparture")
    prediction_time = _parse_departure(prediction_departure) if prediction_departure else None

    def candidate_score(candidate: dict) -> float:
        score = 0.0
        if candidate.get("route_id") and candidate["route_id"] in route_ids:
            score += 8
        if _get_stripped(candidate, "route_short_name") == route_name:
            score += 8

        boarding_stop_id = candidate.get("boarding_stop_id")
        if boarding_stop_id and boarding_stop_id == stop["stop_id"]:
            score += 10
        elif candidate.get("boarding_stop_point"):
            score += max(0, 6 - (_distance_meters(stop, candidate["boarding_stop_point"]) / 40))

        candidate_departure = candidate.get("departure_time")
        if prediction_time and candidate_departure:
            try:
                delta_minutes = abs((_parse_departure(candidate_departure) - prediction_time).total_seconds()) / 60
                score += max(0, 8 - min(delta_minutes, 8))
            except ValueError:
                return score
        return score

    best_candidate = max(google_candidates, key=candidate_score)
    return best_candidate if candidate_score(best_candidate) >= 8 else None


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
    route_name = prediction.get("RouteName", f"Route {index + 1}")
    delay_seconds = int(prediction.get("PredictedDelayInSeconds", 0) or 0)
    delay_minutes = round(delay_seconds / 60)
    eta = _minutes_until_departure(prediction["PredictedDeparture"])
    if eta > ETA_THRESHOLD_MINUTES:
        return None
    prediction_direction_id = _try_int(prediction.get("DirectionId"))
    google_candidate = _select_google_transit_candidate(route_name, stop, prediction, google_candidates)
    try:
        transit_segment = _build_transit_segment(
            route_name=route_name,
            trip_id=prediction.get("TripId"),
            prediction_direction_id=prediction_direction_id,
            boarding_stop_id=boarding_stop_id,
            destination_stop_id=destination_stop_id,
            destination_point=destination_marker,
            google_candidate=google_candidate,
        )
    except HTTPException:
        return None
    if not transit_segment:
        return None

    alighting_stop = transit_segment.get("alighting_stop") or {}
    walk_from_origin = (
        {"lat": alighting_stop["lat"], "lng": alighting_stop["lng"]}
        if alighting_stop
        else {"lat": stop["lat"], "lng": stop["lng"]}
    )
    walk_from_stop = _google_route(walk_from_origin, destination_marker)

    trip_headsign = transit_segment.get("trip_headsign") or _get_any(prediction, "Destination", "DirectionText")
    prediction_headsign = _get_any(prediction, "Destination", "DirectionText")
    if prediction_headsign and not _headsign_is_compatible(trip_headsign, prediction_headsign):
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
    }


def _build_available_buses(
    predictions: List[dict],
    user_position: dict,
    stop: dict,
    destination_marker: dict,
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
) -> List[dict]:
    walk_to_stop = _google_route(user_position, {"lat": stop["lat"], "lng": stop["lng"]})
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
        predictions = _select_route_planning_predictions(predictions, google_candidates)
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
    deduped: Dict[tuple, dict] = {}
    for bus in buses:
        key = (
            bus.get("route_id") or bus.get("route_short_name") or bus.get("route_number"),
            (bus.get("boarding_stop") or {}).get("stop_id"),
            (bus.get("alighting_stop") or {}).get("stop_id"),
        )
        existing = deduped.get(key)
        if existing is None or bus.get("eta", 10**9) < existing.get("eta", 10**9):
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
        key=lambda bus: (bus.get("total_trip_time_minutes", 10**9), bus.get("eta", 10**9)),
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
    direct_walk = None if bus_only else _google_route(start_marker, destination_marker)

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
