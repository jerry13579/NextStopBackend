import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from auth_service import _check_rate_limit, _create_session, _hash_password, _record_login_attempt, _require_user, _user_from_token, _verify_password
from calendar_service import _calendar_commute_previews, _extract_course_code, _filter_and_annotate_calendar_events, _is_future_event, _parse_ics_events, _store_calendar_events
from common import _get_stripped, _parse_departure, _try_int, _validate_lat_lng
from config import ALLOWED_CORS_ORIGINS, GOOGLE_MAPS_API, GOOGLE_PLACES_SEARCH_RADIUS_METERS
from database import _db, _init_db
from google_service import _places_location_bias
from gtfs_service import _gtfs_route_shape_coordinates, _load_gtfs_calendar, _load_gtfs_calendar_dates, _load_gtfs_routes, _load_gtfs_routes_by_id, _load_gtfs_shapes, _load_gtfs_stop_times_by_stop, _load_gtfs_stop_times_by_trip, _load_gtfs_stops_by_id, _load_gtfs_trips, _load_gtfs_trips_by_id, _resolve_route_ids
from http_client import _google_routes_headers, _post_json, _request_json
from local_eats_service import clear_favorite, get_local_eats, set_favorite
from schemas import AuthRequest, AuthResponse, BulkCalendarEventUpdate, CalendarEvent, CalendarImportResponse, CommuteOrigin, DeleteEventsRequest, LocalEatsResponse, ManualEventsRequest, RefreshTokenRequest, Telemetry, TripLogRequest, UserPreferences, UserPreferencesPatch
from telemetry_service import get_bus_metric, store_telemetry
from transit_service import _fetch_predictions, _fetch_route_vehicles, _fetch_service_alert, _minutes_until_departure, build_plan_trip_response
from trip_service import list_trips, log_trip, stats_summary
from user_service import clear_user_data, export_user_data, get_preferences, update_preferences

_init_db()

app = FastAPI(title="NextStop Backend API")


@app.on_event("startup")
async def _startup_prewarm_gtfs():
    try:
        _load_gtfs_trips()
        _load_gtfs_routes()
        _load_gtfs_trips_by_id()
        _load_gtfs_routes_by_id()
        _load_gtfs_stops_by_id()
        _load_gtfs_stop_times_by_trip()
        _load_gtfs_stop_times_by_stop()
        _load_gtfs_shapes()
        _load_gtfs_calendar()
        _load_gtfs_calendar_dates()
    except (HTTPException, FileNotFoundError, OSError, ValueError) as exc:
        print(f"[startup] GTFS pre-warm failed: {exc!r}")


app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_CORS_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {"message": "NextStop backend is running", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", response_model=AuthResponse)
def register_user(data: AuthRequest):
    if not data.email.endswith("@berkeley.edu"):
        raise HTTPException(status_code=400, detail="Use a UC Berkeley email")
    if not _check_rate_limit(data.email):
        raise HTTPException(status_code=429, detail="Too many registration attempts. Try again later.")
    with _db() as connection:
        try:
            cursor = connection.execute(
                "INSERT INTO users (email, name, password_hash, created_at, email_verified) VALUES (?, ?, ?, ?, ?)",
                (data.email, data.name, _hash_password(data.password), datetime.now(timezone.utc).isoformat(), 0),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Account already exists")
    _record_login_attempt(data.email, success=True)
    token = _create_session(cursor.lastrowid)
    return AuthResponse(token=token, email=data.email, name=data.name)


@app.post("/auth/login", response_model=AuthResponse)
def login_user(data: AuthRequest):
    if not _check_rate_limit(data.email):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    with _db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (data.email,)).fetchone()
    if user is None or not _verify_password(data.password, user["password_hash"]):
        _record_login_attempt(data.email, success=False)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    _record_login_attempt(data.email, success=True)
    token = _create_session(user["id"])
    return AuthResponse(token=token, email=user["email"], name=user["name"])


@app.get("/auth/session", response_model=AuthResponse)
def get_session(x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return AuthResponse(token=x_session_token or "", email=user["email"], name=user["name"])


@app.post("/auth/logout")
def logout_user(x_session_token: Optional[str] = Header(default=None)):
    if not x_session_token:
        raise HTTPException(status_code=401, detail="No session token provided")
    with _db() as connection:
        connection.execute("DELETE FROM sessions WHERE token = ?", (x_session_token,))
    return {"status": "success", "message": "Logged out successfully"}


@app.post("/auth/refresh", response_model=AuthResponse)
def refresh_token(data: RefreshTokenRequest):
    user = _user_from_token(data.token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please login again.")
    with _db() as connection:
        connection.execute("DELETE FROM sessions WHERE token = ?", (data.token,))
    new_token = _create_session(user["id"])
    return AuthResponse(token=new_token, email=user["email"], name=user["name"])


@app.post("/telemetry")
def receive_telemetry(data: Telemetry):
    return store_telemetry(data)


@app.get("/bus/{bus_id}")
def get_bus(bus_id: str):
    metric = get_bus_metric(bus_id)
    if metric is None:
        raise HTTPException(status_code=404, detail="Bus not found")
    return metric


@app.get("/next-trip/{stop_id}")
def get_next_trip(stop_id: str):
    next_bus_response = get_next_bus(stop_id)
    next_bus = next_bus_response["next_bus"]
    if next_bus is None:
        return {"stop_id": stop_id, "next_bus": None, "recommendation": "No buses available right now"}
    return {"stop_id": stop_id, "next_bus": next_bus, "recommendation": f"Leave now to catch Route {next_bus['RouteName']}"}


@app.get("/next-bus/{stop_id}")
def get_next_bus(stop_id: str):
    data, source = _fetch_predictions(stop_id)
    if not data:
        return {"status_code": 200, "source": source, "stop_id": stop_id, "next_bus": None}
    next_bus = min(data, key=lambda item: item["PredictedDeparture"])
    return {"status_code": 200, "source": source, "stop_id": stop_id, "next_bus": next_bus}


@app.get("/calendar/events", response_model=CalendarImportResponse)
def get_calendar_events(x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    with _db() as connection:
        rows = connection.execute(
            """
            SELECT external_id, title, location, start_time, end_time, source
            FROM calendar_events
            WHERE user_id = ?
            ORDER BY start_time
            """,
            (user["id"],),
        ).fetchall()
    ics_events = []
    manual_events = []
    for index, row in enumerate(rows):
        event = CalendarEvent(id=row["external_id"] or str(index), title=row["title"], location=row["location"] or "", start_time=row["start_time"], end_time=row["end_time"])
        if row["source"] == "manual":
            manual_events.append(event)
        else:
            ics_events.append(event)
    filtered_ics = _filter_and_annotate_calendar_events(ics_events)
    filtered_manual = [
        CalendarEvent(id=event.id, title=event.title, location=event.location, start_time=event.start_time, end_time=event.end_time, is_course=True, course_code=_extract_course_code(event.title))
        for event in manual_events
        if _is_future_event(event.start_time)
    ]
    all_events = sorted(filtered_ics + filtered_manual, key=lambda event: event.start_time)
    source = rows[0]["source"] if rows else "sqlite"
    return CalendarImportResponse(source=source, events=all_events)


@app.post("/calendar/import", response_model=CalendarImportResponse)
async def import_calendar(file: UploadFile = File(...), commute_origin: Optional[str] = Form(default=None), x_session_token: Optional[str] = Header(default=None)):
    filename = file.filename or "calendar.ics"
    if not filename.lower().endswith(".ics"):
        raise HTTPException(status_code=400, detail="Upload an .ics calendar file")
    content = (await file.read()).decode("utf-8", errors="ignore")
    try:
        events = _filter_and_annotate_calendar_events(_parse_ics_events(content))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Calendar file could not be parsed")
    user = _user_from_token(x_session_token)
    if user is not None:
        _store_calendar_events(user["id"], events, "ics")
    parsed_origin = None
    if commute_origin:
        try:
            parsed_origin = CommuteOrigin(**json.loads(commute_origin))
        except (TypeError, ValueError):
            parsed_origin = None
    return CalendarImportResponse(source="ics", events=events, commute_previews=_calendar_commute_previews(events, parsed_origin))


@app.patch("/calendar/events")
def update_calendar_events(request: BulkCalendarEventUpdate, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    with _db() as connection:
        for item in request.updates:
            if not item.external_ids:
                continue
            set_clauses = []
            params = []
            if item.title is not None:
                set_clauses.append("title = ?")
                params.append(item.title)
            if item.location is not None:
                set_clauses.append("location = ?")
                params.append(item.location)
            if item.start_time is not None:
                set_clauses.append("start_time = ?")
                params.append(item.start_time)
            if item.end_time is not None:
                set_clauses.append("end_time = ?")
                params.append(item.end_time)
            if not set_clauses:
                continue
            placeholders = ",".join("?" for _ in item.external_ids)
            params.extend(item.external_ids)
            params.append(user["id"])
            connection.execute(f"UPDATE calendar_events SET {', '.join(set_clauses)} WHERE external_id IN ({placeholders}) AND user_id = ?", params)
    return {"status": "ok"}


@app.delete("/calendar/events")
def delete_calendar_events(request: DeleteEventsRequest, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    if not request.external_ids:
        return {"status": "ok"}
    with _db() as connection:
        placeholders = ",".join("?" for _ in request.external_ids)
        connection.execute(f"DELETE FROM calendar_events WHERE external_id IN ({placeholders}) AND user_id = ?", [*request.external_ids, user["id"]])
    return {"status": "ok"}


@app.post("/calendar/events", response_model=CalendarImportResponse)
def add_manual_calendar_events(request: ManualEventsRequest, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.executemany(
            """
            INSERT INTO calendar_events
            (user_id, external_id, title, location, start_time, end_time, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(user["id"], event.id, event.title, event.location, event.start_time, event.end_time, "manual", now) for event in request.events],
        )
    return CalendarImportResponse(source="manual", events=request.events, commute_previews=_calendar_commute_previews(request.events, request.commute_origin))


@app.get("/locations/search")
def search_locations(q: str, lat: Optional[float] = None, lng: Optional[float] = None):
    if not GOOGLE_MAPS_API:
        raise HTTPException(status_code=503, detail="Google Maps API key is not configured")
    if not q.strip():
        raise HTTPException(status_code=400, detail="q is required")
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=400, detail="lat and lng must be provided together")
    if lat is not None and lng is not None:
        _validate_lat_lng(lat, lng)
    payload = {"textQuery": q.strip(), "maxResultCount": 6}
    proximity = {"lat": lat, "lng": lng} if lat is not None and lng is not None else None
    bias = _places_location_bias(proximity)
    if bias:
        payload["locationBias"] = bias
    headers = _google_routes_headers("places.id,places.displayName,places.formattedAddress,places.location")
    try:
        data = _post_json("https://places.googleapis.com/v1/places:searchText", payload, headers)
    except requests.RequestException:
        legacy_params = {"query": q.strip(), "key": GOOGLE_MAPS_API}
        if proximity:
            legacy_params["location"] = f"{lat},{lng}"
            legacy_params["radius"] = GOOGLE_PLACES_SEARCH_RADIUS_METERS
        try:
            legacy_data = _request_json("https://maps.googleapis.com/maps/api/place/textsearch/json", legacy_params)
            return {"results": [{"id": place.get("place_id"), "name": place.get("name") or place.get("formatted_address"), "address": place.get("formatted_address"), "lat": place["geometry"]["location"]["lat"], "lng": place["geometry"]["location"]["lng"]} for place in legacy_data.get("results", [])]}
        except requests.RequestException:
            raise HTTPException(status_code=503, detail="Google Places search is unavailable")
    return {"results": [{"id": place.get("id"), "name": place.get("displayName", {}).get("text") or place.get("formattedAddress"), "address": place.get("formattedAddress"), "lat": place["location"]["latitude"], "lng": place["location"]["longitude"]} for place in data.get("places", [])]}


@app.get("/locations/reverse")
def reverse_location(lat: float, lng: float):
    _validate_lat_lng(lat, lng)
    fallback_address = f"{lat:.4f}, {lng:.4f}"
    if not GOOGLE_MAPS_API:
        return {"name": fallback_address, "address": fallback_address}
    try:
        data = _request_json("https://maps.googleapis.com/maps/api/geocode/json", {"latlng": f"{lat},{lng}", "key": GOOGLE_MAPS_API})
        result = (data.get("results") or [None])[0]
        if not result:
            return {"name": fallback_address, "address": fallback_address}
        address = result.get("formatted_address") or fallback_address
        concise_name = None
        for component in result.get("address_components") or []:
            types = component.get("types") or []
            if any(item in types for item in ["premise", "point_of_interest", "establishment", "subpremise"]):
                concise_name = component.get("long_name")
                break
        return {"name": concise_name or (address.split(",")[0].strip() if address else fallback_address), "address": address}
    except requests.RequestException:
        return {"name": fallback_address, "address": fallback_address}


@app.get("/service-alert")
def service_alert(routes: Optional[str] = None):
    route_list = [route.strip() for route in (routes or "").split(",") if route.strip()]
    return {"alert": _fetch_service_alert(route_list)}


@app.get("/config/maps")
def maps_config():
    return {"googleMapsApiKey": GOOGLE_MAPS_API}


@app.get("/gtfs/shape")
def get_gtfs_shape(route_id: str, direction_id: Optional[int] = None):
    coordinates = _gtfs_route_shape_coordinates(route_id, direction_id=direction_id)
    if not coordinates:
        raise HTTPException(status_code=404, detail=f"No GTFS shape found for route_id={route_id}" + ("" if direction_id is None else f", direction_id={direction_id}"))
    return coordinates


def _route_vehicle_matches_prediction_route(prediction: dict, route_id: str, route_ids: set[str]) -> bool:
    prediction_route = _get_stripped(prediction, "RouteName")
    if not prediction_route:
        return False
    if prediction_route == route_id or prediction_route in route_ids:
        return True
    try:
        prediction_route_ids = set(_resolve_route_ids(prediction_route))
    except HTTPException:
        prediction_route_ids = set()
    return route_id in prediction_route_ids or bool(route_ids.intersection(prediction_route_ids))


def _route_vehicle_matches_prediction_direction(prediction: dict, direction_id: Optional[int]) -> bool:
    if direction_id is None:
        return True
    prediction_direction = _try_int(prediction.get("DirectionId"))
    return prediction_direction is not None and prediction_direction == direction_id


def _prediction_departure_matches(prediction: dict, selected_departure_time: Optional[str]) -> bool:
    if not selected_departure_time:
        return True
    prediction_departure = prediction.get("PredictedDeparture")
    if not prediction_departure:
        return False
    try:
        delta_seconds = abs((_parse_departure(prediction_departure) - _parse_departure(selected_departure_time)).total_seconds())
    except (TypeError, ValueError):
        return False
    return delta_seconds <= 120


def _selected_prediction_vehicle_id(
    predictions: list[dict],
    selected_prediction_trip_id: Optional[str],
    selected_departure_time: Optional[str],
) -> Optional[str]:
    selected_trip_id = (selected_prediction_trip_id or "").strip()
    if not selected_trip_id and not selected_departure_time:
        return None

    for prediction in predictions:
        vehicle_id = prediction.get("VehicleId")
        if vehicle_id is None:
            continue
        trip_matches = not selected_trip_id or str(prediction.get("TripId") or "").strip() == selected_trip_id
        if trip_matches and _prediction_departure_matches(prediction, selected_departure_time):
            return str(vehicle_id)
    return None


@app.get("/route/vehicles")
def get_route_vehicles(
    route_id: str,
    direction_id: Optional[int] = None,
    boarding_stop_id: Optional[str] = None,
    selected_vehicle_id: Optional[str] = None,
    selected_prediction_trip_id: Optional[str] = None,
    selected_departure_time: Optional[str] = None,
):
    from gtfs_service import _load_gtfs_trips_by_id
    from telemetry_service import _vehicle_occupancy_for_marker
    vehicles = _fetch_route_vehicles(route_id)
    trips_by_id = _load_gtfs_trips_by_id()
    prediction_by_vehicle = {}
    route_ids = set(_resolve_route_ids(route_id))
    boarding_predictions = []
    if boarding_stop_id:
        predictions, _ = _fetch_predictions(boarding_stop_id)
        for prediction in predictions:
            if not _route_vehicle_matches_prediction_route(prediction, route_id, route_ids):
                continue
            if not _route_vehicle_matches_prediction_direction(prediction, direction_id):
                continue
            boarding_predictions.append(prediction)
            vehicle_id = prediction.get("VehicleId")
            if vehicle_id is not None:
                prediction_by_vehicle[str(vehicle_id)] = prediction
    selected_vehicle_key = (selected_vehicle_id or "").strip() or None
    selected_prediction_vehicle_key = None if selected_vehicle_key else _selected_prediction_vehicle_id(
        boarding_predictions,
        selected_prediction_trip_id,
        selected_departure_time,
    )

    response_vehicles = []
    for vehicle in vehicles:
        if vehicle.get("Latitude") is None or vehicle.get("Longitude") is None:
            continue
        vehicle_key = str(vehicle.get("VehicleId")) if vehicle.get("VehicleId") is not None else None
        current_trip_id = str(vehicle.get("CurrentTripId")) if vehicle.get("CurrentTripId") is not None else None
        current_trip = trips_by_id.get(current_trip_id or "")
        current_trip_direction = _try_int((current_trip or {}).get("direction_id"))
        prediction = prediction_by_vehicle.get(vehicle_key or "")
        exact_selected = bool(selected_vehicle_key and vehicle_key == selected_vehicle_key)
        prediction_selected = bool(
            not exact_selected
            and selected_prediction_vehicle_key
            and vehicle_key == selected_prediction_vehicle_key
        )
        gtfs_direction_verified = bool(
            direction_id is not None
            and current_trip_direction is not None
            and current_trip_direction == direction_id
        )
        if direction_id is not None and not (exact_selected or prediction_selected or gtfs_direction_verified):
            continue
        if exact_selected:
            direction_match_source = "selected_vehicle"
        elif prediction_selected:
            direction_match_source = "boarding_prediction"
        elif gtfs_direction_verified:
            direction_match_source = "gtfs_trip"
        else:
            direction_match_source = None
        is_selected_vehicle = exact_selected or prediction_selected
        arrival_minutes = _minutes_until_departure(prediction["PredictedDeparture"]) if prediction else None
        response_vehicles.append({
            "vehicle_id": vehicle.get("VehicleId"),
            "trip_id": vehicle.get("CurrentTripId"),
            "lat": vehicle.get("Latitude"),
            "lng": vehicle.get("Longitude"),
            "heading": vehicle.get("Heading"),
            "reported_at": vehicle.get("TimeLastReported"),
            "arrival_minutes": arrival_minutes,
            "arrival_source": "boarding_prediction" if prediction else None,
            "headsign": (prediction.get("Destination") or prediction.get("DirectionText")) if prediction else (current_trip or {}).get("trip_headsign"),
            "occupancy_percent": _vehicle_occupancy_for_marker(str(vehicle.get("VehicleId")) if vehicle.get("VehicleId") is not None else None, route_id),
            "is_selected_vehicle": is_selected_vehicle,
            "match_confidence": "exact_vehicle" if exact_selected else "boarding_prediction" if prediction_selected else "route_direction_only",
            "is_direction_verified": bool(direction_match_source),
            "direction_match_source": direction_match_source,
        })

    return sorted(
        response_vehicles,
        key=lambda vehicle: (
            not vehicle["is_selected_vehicle"],
            vehicle["arrival_minutes"] if isinstance(vehicle.get("arrival_minutes"), int) else 10**9,
        ),
    )


@app.get("/plan-trip")
def plan_trip(destination: str, start: str = "Current location", start_lat: Optional[float] = None, start_lng: Optional[float] = None, destination_lat: Optional[float] = None, destination_lng: Optional[float] = None, mode: str = "full", include_walk: bool = True):
    normalized_destination = destination.strip()
    if not normalized_destination:
        raise HTTPException(status_code=400, detail="destination is required")
    if start_lat is None or start_lng is None:
        raise HTTPException(status_code=400, detail="start_lat and start_lng are required")
    _validate_lat_lng(start_lat, start_lng)
    if (destination_lat is None) != (destination_lng is None):
        raise HTTPException(status_code=400, detail="destination_lat and destination_lng must be provided together")
    if destination_lat is not None and destination_lng is not None:
        _validate_lat_lng(destination_lat, destination_lng)
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"full", "bus"}:
        raise HTTPException(status_code=400, detail="mode must be 'full' or 'bus'")
    try:
        return build_plan_trip_response(destination=normalized_destination, start=start.strip() or "Current location", start_lat=start_lat, start_lng=start_lng, destination_lat=destination_lat, destination_lng=destination_lng, mode=normalized_mode, include_walk=include_walk)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[plan_trip] unexpected route planning failure: {exc!r}")
        raise HTTPException(status_code=503, detail="Route planning is temporarily unavailable")


@app.get("/local-eats", response_model=LocalEatsResponse)
def local_eats(category: Optional[str] = None, deals_only: bool = False, favorites_only: bool = False, lat: Optional[float] = None, lng: Optional[float] = None, x_session_token: Optional[str] = Header(default=None)):
    if (lat is None) != (lng is None):
        raise HTTPException(status_code=400, detail="lat and lng must be provided together")
    if lat is not None and lng is not None:
        _validate_lat_lng(lat, lng)
    user = _user_from_token(x_session_token)
    eateries, source = get_local_eats(category, deals_only, favorites_only, lat, lng, user["id"] if user else None)
    return LocalEatsResponse(eateries=eateries, source=source)


@app.put("/local-eats/{eatery_id}/favorite")
def favorite_local_eat(eatery_id: str, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    result = set_favorite(user["id"], eatery_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Eatery not found")
    return result


@app.delete("/local-eats/{eatery_id}/favorite")
def unfavorite_local_eat(eatery_id: str, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return clear_favorite(user["id"], eatery_id)


@app.post("/trips")
def create_trip(request: TripLogRequest, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return log_trip(user["id"], request)


@app.get("/trips")
def trips(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    x_session_token: Optional[str] = Header(default=None),
):
    user = _require_user(x_session_token)
    return {"trips": list_trips(user["id"], from_, to)}


@app.get("/stats/summary")
def summary(period: str = "today", x_session_token: Optional[str] = Header(default=None)):
    if period not in {"today", "week", "semester"}:
        raise HTTPException(status_code=400, detail="period must be today, week, or semester")
    user = _require_user(x_session_token)
    return stats_summary(user["id"], period)


@app.get("/user/preferences", response_model=UserPreferences)
def user_preferences(x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return get_preferences(user["id"])


@app.patch("/user/preferences", response_model=UserPreferences)
def patch_user_preferences(patch: UserPreferencesPatch, x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return update_preferences(user["id"], patch)


@app.get("/user/export")
def user_export(x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return export_user_data(user["id"])


@app.delete("/user/data")
def delete_user_data(x_session_token: Optional[str] = Header(default=None)):
    user = _require_user(x_session_token)
    return clear_user_data(user["id"])
