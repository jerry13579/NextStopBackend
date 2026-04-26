import os
import re
import csv
import json
import hashlib
import secrets
import sqlite3
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

load_dotenv()
ACTRANSIT_API_KEY = os.getenv("ACTRANSIT_API_KEY")
GOOGLE_MAPS_API = os.getenv("GOOGLE_MAPS_API")
AC_TRANSIT_TIMEZONE = ZoneInfo("America/Los_Angeles")
_raw_database_path = os.getenv("DATABASE_PATH", "nextstop.db")
if os.path.isabs(_raw_database_path):
    DATABASE_PATH = _raw_database_path
else:
    DATABASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), _raw_database_path)
_raw_gtfs_static_dir = os.getenv("GTFS_STATIC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtfs"))
if os.path.isabs(_raw_gtfs_static_dir):
    GTFS_STATIC_DIR = _raw_gtfs_static_dir
else:
    GTFS_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), _raw_gtfs_static_dir)

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
    except Exception:
        pass  # GTFS files may not be present in all environments

default_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "capacitor://localhost",
    "ionic://localhost",
    "http://localhost",
    "https://localhost",
]

configured_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(default_origins + configured_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

latest_bus_metrics: Dict[str, dict] = {}
vehicle_attributes_cache: Dict[str, tuple[datetime, Optional[dict]]] = {}
VEHICLE_ATTRIBUTES_TTL_SECONDS = 90
ROUTE_PLAN_MAX_STOPS = 4
ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP = 2
ROUTE_PLAN_MAX_TOTAL_OPTIONS = 8
CALENDAR_COMMUTE_PREVIEW_EVENT_LIMIT = 3
CALENDAR_COMMUTE_PREVIEW_STOP_LIMIT = 2
CALENDAR_COMMUTE_PREVIEW_STOP_TIME_LIMIT = 4

# Session configuration
SESSION_EXPIRY_HOURS = 24
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_MINUTES = 15


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                email_verified INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                success INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                external_id TEXT,
                title TEXT NOT NULL,
                location TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )


_init_db()


class Telemetry(BaseModel):
    bus_id: str
    crowding_percentage: float
    passenger_count: int
    seats_available: int
    timestamp: Optional[str] = None
    location: Optional[str] = None
    device_status: Optional[str] = None


class NextClass(BaseModel):
    course: str
    location: str
    start_time: str
    minutes_until: int


class NearestStop(BaseModel):
    stop_name: str
    walking_minutes: int


class AvailableBus(BaseModel):
    route: str
    arrival_in_min: int
    capacity: int
    seats_left: int
    current_stop: str
    recommendation: str


class NextTripResponse(BaseModel):
    next_class: NextClass
    nearest_stop: NearestStop
    available_buses: List[AvailableBus]
    recommendation: str
    confidence: float
    message: str


class CalendarEvent(BaseModel):
    id: str
    title: str
    location: str
    start_time: str
    end_time: Optional[str] = None
    is_course: bool = False
    course_code: Optional[str] = None


class CommuteOrigin(BaseModel):
    name: Optional[str] = None
    lat: float
    lng: float


class CalendarCommuteOption(BaseModel):
    type: str
    route_number: Optional[str] = None
    route_name: Optional[str] = None
    leave_by: str
    depart_at: Optional[str] = None
    arrive_at: str
    total_trip_time_minutes: int
    walk_time_minutes: int
    ride_time_minutes: int = 0
    destination_walk_time_minutes: int = 0
    boarding_stop_name: Optional[str] = None
    alighting_stop_name: Optional[str] = None


class CalendarCommutePreview(BaseModel):
    event_id: str
    origin_name: Optional[str] = None
    destination: str
    source: str
    leave_by: Optional[str] = None
    best_route_number: Optional[str] = None
    options: List[CalendarCommuteOption] = []


class CalendarImportResponse(BaseModel):
    source: str
    events: List[CalendarEvent]
    commute_previews: Optional[List[CalendarCommutePreview]] = None


class AuthRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class AuthResponse(BaseModel):
    token: str
    email: str
    name: Optional[str] = None


def _hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${password_hash}"


def _verify_password(password: str, stored_hash: str) -> bool:
    salt, _ = stored_hash.split("$", 1)
    return secrets.compare_digest(_hash_password(password, salt), stored_hash)


def _create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_EXPIRY_HOURS)
    with _db() as connection:
        connection.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires_at.isoformat()),
        )
    return token


def _user_from_token(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    with _db() as connection:
        return connection.execute(
            """
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ? AND sessions.expires_at > ?
            """,
            (token, datetime.now(timezone.utc).isoformat()),
        ).fetchone()


def _require_user(token: Optional[str]) -> sqlite3.Row:
    user = _user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please login again.")
    return user


def _check_rate_limit(email: str) -> bool:
    """Check if user has exceeded login attempt limit."""
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)
    with _db() as connection:
        count = connection.execute(
            "SELECT COUNT(*) as cnt FROM login_attempts WHERE email = ? AND attempted_at > ? AND success = 0",
            (email, cutoff_time.isoformat()),
        ).fetchone()
    return count["cnt"] < MAX_LOGIN_ATTEMPTS


def _record_login_attempt(email: str, success: bool = False) -> None:
    """Record a login attempt for rate limiting."""
    with _db() as connection:
        connection.execute(
            "INSERT INTO login_attempts (email, attempted_at, success) VALUES (?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), 1 if success else 0),
        )


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
    lat_delta = (point_a["lat"] - point_b["lat"]) * 111_320
    lng_delta = (point_a["lng"] - point_b["lng"]) * 111_320
    return (lat_delta ** 2 + lng_delta ** 2) ** 0.5


def _request_json(url: str, params: Optional[dict] = None) -> Any:
    response = requests.get(url, params=params, timeout=6)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, payload: dict, headers: Optional[dict] = None) -> Any:
    response = requests.post(url, json=payload, headers=headers, timeout=6)
    response.raise_for_status()
    return response.json()


def _fetch_service_alert(routes: Optional[List[str]] = None) -> Optional[dict]:
    if not ACTRANSIT_API_KEY:
        return None

    route_list = [route.strip() for route in (routes or []) if route and route.strip()]
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
        brief = (bulletin.get("brf") or bulletin.get("sbj") or bulletin.get("dtl") or "").strip()
        if not brief or brief.strip().lower() in generic_alerts:
            continue

        services = bulletin.get("srvc") or []
        affected_routes = sorted({
            str(service.get("rt") or "").strip()
            for service in services
            if str(service.get("rt") or "").strip()
        })
        if not affected_routes:
            continue
        if requested_routes and not requested_routes.intersection({route.upper() for route in affected_routes}):
            continue

        first_service = services[0] if services else {}
        return {
            "summary": brief,
            "subject": (bulletin.get("sbj") or "").strip() or None,
            "effective": (bulletin.get("efct") or "").strip() or None,
            "route": affected_routes[0],
            "routes": affected_routes,
            "direction": (first_service.get("rtdir") or "").strip() or None,
            "stop_name": (first_service.get("stpnm") or "").strip() or None,
        }

    return None


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


def _occupancy_from_status_color(color: Optional[str]) -> Optional[int]:
    if not color:
        return None

    normalized = color.strip().lower().lstrip("#")
    color_map = {
        "00ff00": 10,
        "008000": 10,
        "00843d": 10,
        "green": 10,
        "ffa500": 50,
        "ff9900": 50,
        "ff8c00": 50,
        "orange": 50,
        "ff0000": 90,
        "c00000": 90,
        "red": 90,
    }
    if normalized in color_map:
        return color_map[normalized]

    if len(normalized) == 6:
        try:
            red = int(normalized[0:2], 16)
            green = int(normalized[2:4], 16)
            blue = int(normalized[4:6], 16)
        except ValueError:
            return None

        if red >= 200 and green >= 80 and blue <= 100:
            return 50
        if green >= red and green >= blue:
            return 10
        if red >= green and red >= blue:
            return 90
        return 50

    return None


def _fetch_vehicle_realtime_attributes(vehicle_id: str, route_name: str) -> Optional[dict]:
    if not ACTRANSIT_API_KEY:
        return None

    cache_key = f"{route_name}:{vehicle_id}"
    now = datetime.now(timezone.utc)
    cached = vehicle_attributes_cache.get(cache_key)
    if cached and (now - cached[0]).total_seconds() < VEHICLE_ATTRIBUTES_TTL_SECONDS:
        return cached[1]

    try:
        data = _request_json(
            "https://api.actransit.org/transit/vehicle/realtimeattributes",
            {
                "vehicleId": vehicle_id,
                "routename": route_name,
                "token": ACTRANSIT_API_KEY,
            },
        )
        if isinstance(data, list):
            attributes = data[0] if data else None
        else:
            attributes = data or None
        vehicle_attributes_cache[cache_key] = (now, attributes)
        return attributes
    except requests.RequestException:
        vehicle_attributes_cache[cache_key] = (now, None)
        return None


def _gtfs_file_path(filename: str) -> str:
    return os.path.join(GTFS_STATIC_DIR, filename)


def _ensure_gtfs_files_exist() -> None:
    required = ["routes.txt", "trips.txt", "stop_times.txt", "stops.txt", "shapes.txt"]
    missing = [name for name in required if not os.path.exists(_gtfs_file_path(name))]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"GTFS static files missing: {', '.join(missing)}. Expected under {GTFS_STATIC_DIR}",
        )


@lru_cache(maxsize=1)
def _load_gtfs_trips() -> List[dict]:
    _ensure_gtfs_files_exist()
    with open(_gtfs_file_path("trips.txt"), newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


@lru_cache(maxsize=1)
def _load_gtfs_routes() -> List[dict]:
    _ensure_gtfs_files_exist()
    with open(_gtfs_file_path("routes.txt"), newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


@lru_cache(maxsize=1)
def _load_gtfs_trips_by_id() -> Dict[str, dict]:
    return {
        trip_id: row
        for row in _load_gtfs_trips()
        if (trip_id := (row.get("trip_id") or "").strip())
    }


@lru_cache(maxsize=1)
def _load_gtfs_routes_by_id() -> Dict[str, dict]:
    return {
        route_id: row
        for row in _load_gtfs_routes()
        if (route_id := (row.get("route_id") or "").strip())
    }


@lru_cache(maxsize=1)
def _load_gtfs_stops_by_id() -> Dict[str, dict]:
    _ensure_gtfs_files_exist()
    stops: Dict[str, dict] = {}
    with open(_gtfs_file_path("stops.txt"), newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            stop_id = (row.get("stop_id") or "").strip()
            if not stop_id:
                continue
            lat_raw = row.get("stop_lat")
            lng_raw = row.get("stop_lon")
            if lat_raw is None or lng_raw is None:
                continue
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
            except ValueError:
                continue
            stops[stop_id] = {
                "stop_id": stop_id,
                "stop_name": row.get("stop_name") or stop_id,
                "lat": lat,
                "lng": lng,
            }
    return stops


def _normalize_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _text_tokens(value: Optional[str]) -> set[str]:
    return {
        token
        for token in _normalize_text(value).split()
        if len(token) >= 3 and token not in {"the", "and", "via", "toward", "towards", "route"}
    }


def _headsign_is_compatible(candidate_headsign: Optional[str], expected_headsign: Optional[str]) -> bool:
    expected = _normalize_text(expected_headsign)
    if not expected:
        return True
    candidate = _normalize_text(candidate_headsign)
    if not candidate:
        return False
    if candidate == expected or candidate in expected or expected in candidate:
        return True
    return bool(_text_tokens(candidate) & _text_tokens(expected))


def _segment_moves_toward_destination(segment: dict, destination_point: dict) -> bool:
    boarding_stop = segment.get("boarding_stop") or {}
    alighting_stop = segment.get("alighting_stop") or {}
    if not boarding_stop or not alighting_stop:
        return False
    boarding_distance = _distance_meters(destination_point, boarding_stop)
    alighting_distance = _distance_meters(destination_point, alighting_stop)
    return alighting_distance < boarding_distance


def _nearest_gtfs_stop_id(point: dict) -> Optional[str]:
    try:
        stops_by_id = _load_gtfs_stops_by_id()
    except HTTPException:
        return None
    if not stops_by_id:
        return None

    best_stop_id: Optional[str] = None
    best_distance = float("inf")
    for stop_id, stop in stops_by_id.items():
        distance = _distance_meters(point, {"lat": stop["lat"], "lng": stop["lng"]})
        if distance < best_distance:
            best_distance = distance
            best_stop_id = stop_id
    return best_stop_id


def _nearest_gtfs_stop(point: dict, preferred_name: Optional[str] = None) -> Optional[dict]:
    stops_by_id = _load_gtfs_stops_by_id()
    if not stops_by_id:
        return None

    normalized_preferred_name = _normalize_text(preferred_name)
    best_stop: Optional[dict] = None
    best_score = float("inf")
    for stop in stops_by_id.values():
        distance = _distance_meters(point, {"lat": stop["lat"], "lng": stop["lng"]})
        score = distance
        normalized_stop_name = _normalize_text(stop["stop_name"])
        if normalized_preferred_name:
            if normalized_stop_name == normalized_preferred_name:
                score -= 200
            elif normalized_preferred_name in normalized_stop_name or normalized_stop_name in normalized_preferred_name:
                score -= 120
            else:
                score += 120
        if score < best_score:
            best_score = score
            best_stop = stop
    return best_stop


def _nearest_gtfs_stops(point: dict, limit: int = 4, max_distance_meters: int = 1800) -> List[dict]:
    stops_by_id = _load_gtfs_stops_by_id()
    ranked: List[tuple[float, dict]] = []
    for stop in stops_by_id.values():
        distance = _distance_meters(point, {"lat": stop["lat"], "lng": stop["lng"]})
        if distance <= max_distance_meters:
            ranked.append((distance, stop))
    return [stop for _, stop in sorted(ranked, key=lambda item: item[0])[:limit]]


def _parse_gtfs_time_to_seconds(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


@lru_cache(maxsize=1)
def _load_gtfs_stop_times_by_trip() -> Dict[str, List[dict]]:
    _ensure_gtfs_files_exist()
    by_trip: Dict[str, List[dict]] = {}
    with open(_gtfs_file_path("stop_times.txt"), newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            trip_id = (row.get("trip_id") or "").strip()
            stop_id = (row.get("stop_id") or "").strip()
            seq_raw = row.get("stop_sequence")
            if not trip_id or not stop_id or seq_raw is None:
                continue
            try:
                stop_sequence = int(float(str(seq_raw).strip()))
            except ValueError:
                continue
            arrival_time = (row.get("arrival_time") or "").strip()
            departure_time = (row.get("departure_time") or "").strip()
            shape_distance_raw = row.get("shape_dist_traveled")
            try:
                shape_dist_traveled = float(shape_distance_raw) if shape_distance_raw not in (None, "") else None
            except ValueError:
                shape_dist_traveled = None
            by_trip.setdefault(trip_id, []).append(
                {
                    "stop_id": stop_id,
                    "stop_sequence": stop_sequence,
                    "arrival_time": arrival_time or None,
                    "departure_time": departure_time or None,
                    "arrival_seconds": _parse_gtfs_time_to_seconds(arrival_time),
                    "departure_seconds": _parse_gtfs_time_to_seconds(departure_time),
                    "shape_dist_traveled": shape_dist_traveled,
                }
            )
    for stop_times in by_trip.values():
        stop_times.sort(key=lambda item: item["stop_sequence"])
    return by_trip


@lru_cache(maxsize=1)
def _load_gtfs_stop_times_by_stop() -> Dict[str, List[dict]]:
    by_stop: Dict[str, List[dict]] = {}
    for trip_id, stop_times in _load_gtfs_stop_times_by_trip().items():
        for stop_time in stop_times:
            stop_id = stop_time["stop_id"]
            by_stop.setdefault(stop_id, []).append(
                {
                    "trip_id": trip_id,
                    **stop_time,
                }
            )
    for stop_times in by_stop.values():
        stop_times.sort(key=lambda item: (item.get("departure_seconds") or item.get("arrival_seconds") or 10**9))
    return by_stop


@lru_cache(maxsize=1)
def _load_gtfs_shapes() -> Dict[str, List[dict]]:
    _ensure_gtfs_files_exist()
    shapes_by_id: Dict[str, List[dict]] = {}
    with open(_gtfs_file_path("shapes.txt"), newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            shape_id = (row.get("shape_id") or "").strip()
            if not shape_id:
                continue
            lat_raw = row.get("shape_pt_lat")
            lng_raw = row.get("shape_pt_lon")
            sequence_raw = row.get("shape_pt_sequence") or "0"
            if lat_raw is None or lng_raw is None:
                continue
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
                sequence = int(float(sequence_raw))
            except ValueError:
                continue
            shapes_by_id.setdefault(shape_id, []).append(
                {"lat": lat, "lng": lng, "sequence": sequence}
            )
    for shape_points in shapes_by_id.values():
        shape_points.sort(key=lambda point: point["sequence"])
    return shapes_by_id


@lru_cache(maxsize=1)
def _gtfs_trip_directions() -> Dict[str, Optional[int]]:
    directions: Dict[str, Optional[int]] = {}
    for row in _load_gtfs_trips():
        trip_id = (row.get("trip_id") or "").strip()
        if not trip_id:
            continue
        direction_raw = row.get("direction_id")
        if direction_raw is None or str(direction_raw).strip() == "":
            directions[trip_id] = None
            continue
        try:
            directions[trip_id] = int(float(str(direction_raw).strip()))
        except ValueError:
            directions[trip_id] = None
    return directions


@lru_cache(maxsize=1)
def _load_gtfs_calendar() -> List[dict]:
    path = _gtfs_file_path("calendar.txt")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


@lru_cache(maxsize=1)
def _load_gtfs_calendar_dates() -> Dict[str, List[dict]]:
    path = _gtfs_file_path("calendar_dates.txt")
    if not os.path.exists(path):
        return {}
    by_date: Dict[str, List[dict]] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            date_key = (row.get("date") or "").strip()
            if not date_key:
                continue
            by_date.setdefault(date_key, []).append(row)
    return by_date


def _resolve_route_ids(route_identifier: str) -> List[str]:
    route_key = route_identifier.strip()
    if not route_key:
        return []
    routes = _load_gtfs_routes()
    route_ids = [
        (row.get("route_id") or "").strip()
        for row in routes
        if (row.get("route_id") or "").strip() == route_key
    ]
    if route_ids:
        return route_ids
    return [
        (row.get("route_id") or "").strip()
        for row in routes
        if (row.get("route_short_name") or "").strip() == route_key
    ]


def _nearest_shape_index(shape_points: List[dict], point: dict) -> int:
    if not shape_points:
        return -1
    best_index = -1
    best_distance = float("inf")
    for index, shape_point in enumerate(shape_points):
        distance = _distance_meters(
            {"lat": shape_point["lat"], "lng": shape_point["lng"]},
            {"lat": point["lat"], "lng": point["lng"]},
        )
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _segment_stops_for_trip(
    trip_stop_times: List[dict],
    stops_by_id: Dict[str, dict],
    boarding_sequence: int,
    alighting_sequence: int,
    boarding_stop_id: str,
    alighting_stop_id: str,
) -> List[dict]:
    segment: List[dict] = []
    for stop_time in trip_stop_times:
        sequence = stop_time["stop_sequence"]
        if sequence < boarding_sequence or sequence > alighting_sequence:
            continue
        stop = stops_by_id.get(stop_time["stop_id"])
        if not stop:
            continue
        segment.append(
            {
                "stop_id": stop["stop_id"],
                "stop_name": stop["stop_name"],
                "lat": stop["lat"],
                "lng": stop["lng"],
                "stop_sequence": sequence,
                "is_boarding": stop["stop_id"] == boarding_stop_id,
                "is_alighting": stop["stop_id"] == alighting_stop_id,
            }
        )
    return segment


def _pick_boarding_stop(
    trip_stop_times: List[dict],
    stops_by_id: Dict[str, dict],
    preferred_stop_id: str,
) -> Optional[dict]:
    for stop_time in trip_stop_times:
        if stop_time["stop_id"] == preferred_stop_id:
            stop = stops_by_id.get(preferred_stop_id)
            if stop:
                return {
                    "stop_id": preferred_stop_id,
                    "stop_sequence": stop_time["stop_sequence"],
                    "arrival_time": stop_time.get("arrival_time"),
                    "departure_time": stop_time.get("departure_time"),
                    "arrival_seconds": stop_time.get("arrival_seconds"),
                    "departure_seconds": stop_time.get("departure_seconds"),
                    **stop,
                }
    return None


def _ride_minutes_between(boarding_stop: dict, alighting_stop: dict) -> int:
    departure_seconds = boarding_stop.get("departure_seconds")
    if departure_seconds is None:
        departure_seconds = boarding_stop.get("arrival_seconds")
    arrival_seconds = alighting_stop.get("arrival_seconds")
    if arrival_seconds is None:
        arrival_seconds = alighting_stop.get("departure_seconds")

    if departure_seconds is not None and arrival_seconds is not None and arrival_seconds >= departure_seconds:
        return max(round((arrival_seconds - departure_seconds) / 60), 1)

    stop_delta = max(alighting_stop["stop_sequence"] - boarding_stop["stop_sequence"], 1)
    return max(stop_delta * 2, 1)


def _pick_alighting_stop(
    trip_stop_times: List[dict],
    stops_by_id: Dict[str, dict],
    destination_stop_id: Optional[str],
    preferred_alighting_stop_id: Optional[str],
    destination_point: dict,
    boarding_stop: dict,
) -> Optional[dict]:
    best: Optional[dict] = None
    best_score = float("inf")
    for stop_time in trip_stop_times:
        if stop_time["stop_sequence"] <= boarding_stop["stop_sequence"]:
            continue
        stop = stops_by_id.get(stop_time["stop_id"])
        if not stop:
            continue
        candidate = {
            "stop_id": stop["stop_id"],
            "stop_sequence": stop_time["stop_sequence"],
            "arrival_time": stop_time.get("arrival_time"),
            "departure_time": stop_time.get("departure_time"),
            "arrival_seconds": stop_time.get("arrival_seconds"),
            "departure_seconds": stop_time.get("departure_seconds"),
            **stop,
        }
        walk_distance = _distance_meters(destination_point, {"lat": stop["lat"], "lng": stop["lng"]})
        walk_minutes = max(round(walk_distance / 80), 1)
        ride_minutes = _ride_minutes_between(boarding_stop, candidate)
        score = ride_minutes + walk_minutes
        if destination_stop_id and stop["stop_id"] == destination_stop_id:
            score -= 12
        if preferred_alighting_stop_id and stop["stop_id"] == preferred_alighting_stop_id:
            score -= 16
        if score < best_score:
            best_score = score
            best = candidate
    return best


def _evaluate_trip_segment(
    trip: dict,
    stops_by_id: Dict[str, dict],
    stop_times_by_trip: Dict[str, List[dict]],
    shapes_by_id: Dict[str, List[dict]],
    boarding_stop_id: str,
    destination_stop_id: Optional[str],
    preferred_alighting_stop_id: Optional[str],
    destination_point: dict,
    preferred_segment_geometry: Optional[dict] = None,
) -> Optional[dict]:
    trip_id = (trip.get("trip_id") or "").strip()
    shape_id = (trip.get("shape_id") or "").strip()
    if not trip_id or not shape_id:
        return None

    trip_stop_times = stop_times_by_trip.get(trip_id, [])
    shape_points = shapes_by_id.get(shape_id, [])
    if len(trip_stop_times) < 2 or len(shape_points) < 2:
        return None

    boarding_stop = _pick_boarding_stop(trip_stop_times, stops_by_id, boarding_stop_id)
    if not boarding_stop:
        return None
    alighting_stop = _pick_alighting_stop(
        trip_stop_times,
        stops_by_id,
        destination_stop_id,
        preferred_alighting_stop_id,
        destination_point,
        boarding_stop,
    )
    if not alighting_stop:
        return None

    boarding_before_alighting = boarding_stop["stop_sequence"] < alighting_stop["stop_sequence"]
    if not boarding_before_alighting:
        return None

    boarding_shape_index = _nearest_shape_index(shape_points, boarding_stop)
    alighting_shape_index = _nearest_shape_index(shape_points, alighting_stop)
    if boarding_shape_index < 0 or alighting_shape_index < 0:
        return None

    if boarding_shape_index > alighting_shape_index:
        return None

    shape_segment_points = shape_points[boarding_shape_index: alighting_shape_index + 1]
    if len(shape_segment_points) < 2:
        return None

    segment_stops = _segment_stops_for_trip(
        trip_stop_times,
        stops_by_id,
        boarding_stop["stop_sequence"],
        alighting_stop["stop_sequence"],
        boarding_stop["stop_id"],
        alighting_stop["stop_id"],
    )

    full_geometry = {
        "type": "LineString",
        "coordinates": [
            [point["lng"], point["lat"]]
            for point in shape_points
        ],
    }
    segment_geometry = preferred_segment_geometry or {
        "type": "LineString",
        "coordinates": [
            [point["lng"], point["lat"]]
            for point in shape_segment_points
        ],
    }

    trip_headsign = (trip.get("trip_headsign") or "").strip() or None
    route_id = (trip.get("route_id") or "").strip()
    route = _load_gtfs_routes_by_id().get(route_id, {})

    return {
        "trip_id": trip_id,
        "route_id": route_id,
        "route_short_name": (route.get("route_short_name") or route_id).strip() or route_id,
        "route_long_name": (route.get("route_long_name") or route.get("route_short_name") or route_id).strip() or route_id,
        "direction_id": int(trip.get("direction_id")) if str(trip.get("direction_id", "")).strip().isdigit() else None,
        "shape_id": shape_id,
        "trip_headsign": trip_headsign,
        "boarding_stop": {
            "stop_id": boarding_stop["stop_id"],
            "stop_name": boarding_stop["stop_name"],
            "lat": boarding_stop["lat"],
            "lng": boarding_stop["lng"],
            "stop_sequence": boarding_stop["stop_sequence"],
        },
        "alighting_stop": {
            "stop_id": alighting_stop["stop_id"],
            "stop_name": alighting_stop["stop_name"],
            "lat": alighting_stop["lat"],
            "lng": alighting_stop["lng"],
            "stop_sequence": alighting_stop["stop_sequence"],
        },
        "boarding_before_alighting": True,
        "ride_time_minutes": _ride_minutes_between(boarding_stop, alighting_stop),
        "segment_stops": segment_stops,
        "geometry": segment_geometry,
        "geometry_source": "google_transit" if preferred_segment_geometry else "gtfs_static",
        "full_route_geometry": full_geometry,
    }


def _build_transit_segment(
    route_name: str,
    trip_id: Optional[int],
    prediction_direction_id: Optional[int],
    boarding_stop_id: str,
    destination_stop_id: Optional[str],
    destination_point: dict,
    google_candidate: Optional[dict] = None,
) -> Optional[dict]:
    _ensure_gtfs_files_exist()
    trips = _load_gtfs_trips()
    stop_times_by_trip = _load_gtfs_stop_times_by_trip()
    shapes_by_id = _load_gtfs_shapes()
    stops_by_id = _load_gtfs_stops_by_id()

    selected_trip: Optional[dict] = None
    if trip_id is not None:
        trip_id_str = str(trip_id)
        selected_trip = next(
            (trip for trip in trips if (trip.get("trip_id") or "").strip() == trip_id_str),
            None,
        )

    route_ids = _resolve_route_ids(route_name)
    google_route_id = (google_candidate or {}).get("route_id")
    google_route_short_name = (google_candidate or {}).get("route_short_name")
    if google_route_id and google_route_id not in route_ids:
        route_ids = [google_route_id, *route_ids]
    if not route_ids and google_route_short_name:
        route_ids = _resolve_route_ids(google_route_short_name)

    candidate_trips = [
        trip
        for trip in trips
        if (trip.get("route_id") or "").strip() in route_ids
    ]

    direction_hint: Optional[int] = None
    if selected_trip and str(selected_trip.get("direction_id", "")).strip().isdigit():
        direction_hint = int(selected_trip.get("direction_id"))
    elif prediction_direction_id is not None:
        direction_hint = prediction_direction_id

    preferred_headsign = _normalize_text((google_candidate or {}).get("trip_headsign"))
    prioritized: List[tuple[int, dict]] = []
    selected_trip_route_id = (selected_trip.get("route_id") or "").strip() if selected_trip else ""
    if selected_trip and (not route_ids or selected_trip_route_id in route_ids):
        prioritized.append((100, selected_trip))

    for trip in candidate_trips:
        score = 0
        trip_direction_raw = str(trip.get("direction_id", "")).strip()
        trip_headsign = _normalize_text(trip.get("trip_headsign"))
        if direction_hint is not None and trip_direction_raw.isdigit():
            score += 30 if int(trip_direction_raw) == direction_hint else -10
        if preferred_headsign:
            if trip_headsign == preferred_headsign:
                score += 35
            elif preferred_headsign in trip_headsign or trip_headsign in preferred_headsign:
                score += 18
        if google_route_id and (trip.get("route_id") or "").strip() == google_route_id:
            score += 12
        prioritized.append((score, trip))

    seen = set()
    ordered_candidates: List[dict] = []
    for _, trip in sorted(prioritized, key=lambda item: item[0], reverse=True):
        candidate_id = (trip.get("trip_id") or "").strip()
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        ordered_candidates.append(trip)

    preferred_alighting_stop_id = (google_candidate or {}).get("alighting_stop_id") or destination_stop_id
    preferred_segment_geometry = (google_candidate or {}).get("segment_geometry")
    expected_boarding_stop_id = (google_candidate or {}).get("boarding_stop_id")
    expected_alighting_stop_id = (google_candidate or {}).get("alighting_stop_id")

    for trip in ordered_candidates:
        segment = _evaluate_trip_segment(
            trip,
            stops_by_id,
            stop_times_by_trip,
            shapes_by_id,
            boarding_stop_id=boarding_stop_id,
            destination_stop_id=destination_stop_id,
            preferred_alighting_stop_id=preferred_alighting_stop_id,
            destination_point=destination_point,
            preferred_segment_geometry=preferred_segment_geometry,
        )
        if not segment:
            continue
        if preferred_headsign and not _headsign_is_compatible(segment.get("trip_headsign"), preferred_headsign):
            continue
        if not _segment_moves_toward_destination(segment, destination_point):
            continue
        if expected_boarding_stop_id and segment["boarding_stop"]["stop_id"] != expected_boarding_stop_id:
            continue
        if expected_alighting_stop_id and segment["alighting_stop"]["stop_id"] != expected_alighting_stop_id:
            if preferred_segment_geometry:
                continue
        return segment

    for trip in ordered_candidates:
        segment = _evaluate_trip_segment(
            trip,
            stops_by_id,
            stop_times_by_trip,
            shapes_by_id,
            boarding_stop_id=boarding_stop_id,
            destination_stop_id=destination_stop_id,
            preferred_alighting_stop_id=destination_stop_id,
            destination_point=destination_point,
        )
        if not segment:
            continue
        if preferred_headsign and not _headsign_is_compatible(segment.get("trip_headsign"), preferred_headsign):
            continue
        if _segment_moves_toward_destination(segment, destination_point):
            return segment
    return None


def _gtfs_route_shape_coordinates(route_id: str, direction_id: Optional[int] = None) -> List[dict]:
    route_key = route_id.strip()
    if not route_key:
        raise HTTPException(status_code=400, detail="route_id is required")

    trips = _load_gtfs_trips()
    matching_trips = [
        row
        for row in trips
        if (row.get("route_id") or "").strip() == route_key
    ]

    # Helpful fallback when caller passes route short-name instead of route_id.
    if not matching_trips:
        route_lookup_path = _gtfs_file_path("routes.txt")
        if os.path.exists(route_lookup_path):
            with open(route_lookup_path, newline="", encoding="utf-8-sig") as handle:
                routes = list(csv.DictReader(handle))
            route_ids = [
                (row.get("route_id") or "").strip()
                for row in routes
                if (row.get("route_short_name") or "").strip() == route_key
            ]
            if route_ids:
                matching_trips = [
                    row
                    for row in trips
                    if (row.get("route_id") or "").strip() in route_ids
                ]

    if direction_id is not None:
        matching_trips = [
            row
            for row in matching_trips
            if row.get("direction_id") is not None
            and str(row.get("direction_id")).strip() == str(direction_id)
        ]

    if not matching_trips:
        return []

    shape_ids = [
        (row.get("shape_id") or "").strip()
        for row in matching_trips
        if (row.get("shape_id") or "").strip()
    ]
    if not shape_ids:
        return []

    shape_id = Counter(shape_ids).most_common(1)[0][0]
    shapes_by_id = _load_gtfs_shapes()
    shape_points = shapes_by_id.get(shape_id, [])
    return [{"lat": point["lat"], "lng": point["lng"]} for point in shape_points]


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


def _vehicle_occupancy_for_marker(vehicle_id: Optional[str], route_name: str) -> Optional[int]:
    if not vehicle_id:
        return None
    metrics = latest_bus_metrics.get(str(vehicle_id)) or latest_bus_metrics.get(vehicle_id)
    if metrics and metrics.get("crowding_percentage") is not None:
        return round(float(metrics["crowding_percentage"]))

    attributes = _fetch_vehicle_realtime_attributes(str(vehicle_id), route_name)
    if not attributes:
        return None
    percentage = attributes.get("EstimatedOccupancyPercentage")
    if percentage is not None:
        try:
            return round(float(percentage))
        except (TypeError, ValueError):
            return None
    return _occupancy_from_status_color(attributes.get("EstimatedOccupancyStatusColor"))


def _fetch_trip_pattern(route_name: str, trip_id: Optional[int]) -> Optional[dict]:
    try:
        gtfs_coordinates = _gtfs_route_shape_coordinates(route_name, direction_id=None)
        if len(gtfs_coordinates) >= 2:
            return {
                "type": "LineString",
                "coordinates": [[point["lng"], point["lat"]] for point in gtfs_coordinates],
                "source": "gtfs_static",
            }
    except HTTPException:
        pass

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
        if lat is not None and lng is not None:
            coordinates.append([float(lng), float(lat)])

    if len(coordinates) < 2:
        return None
    return {"type": "LineString", "coordinates": coordinates}


def _capacity_for_prediction(prediction: dict, route_name: str) -> tuple[Optional[int], Optional[dict]]:
    vehicle_id = prediction.get("VehicleId")
    if vehicle_id is None:
        return None, None

    metrics = latest_bus_metrics.get(str(vehicle_id)) or latest_bus_metrics.get(vehicle_id)
    if metrics:
        capacity = metrics.get("crowding_percentage")
        if capacity is not None:
            return round(float(capacity)), {
                "source": "telemetry",
                "vehicle_id": str(vehicle_id),
                "estimated_occupancy_status_color": None,
                "estimated_occupancy_status": None,
                "estimated_occupancy_percentage": round(float(capacity)),
            }

    attributes = _fetch_vehicle_realtime_attributes(str(vehicle_id), route_name)
    if not attributes:
        return None, None

    occupancy_color = attributes.get("EstimatedOccupancyStatusColor")
    mapped_capacity = _occupancy_from_status_color(occupancy_color)
    if mapped_capacity is None and attributes.get("EstimatedOccupancyPercentage") is not None:
        mapped_capacity = round(float(attributes["EstimatedOccupancyPercentage"]))

    return mapped_capacity, {
        "source": "actransit_realtimeattributes",
        "vehicle_id": attributes.get("VehicleId") or str(vehicle_id),
        "estimated_occupancy_status_color": occupancy_color,
        "estimated_occupancy_status": attributes.get("EstimatedOccupancyStatus"),
        "estimated_occupancy_percentage": attributes.get("EstimatedOccupancyPercentage"),
        "vehicle_capacity": attributes.get("VehicleCapacity"),
        "current_passenger_count": attributes.get("CurrentPassengerCount"),
        "reported_at": attributes.get("DateTimeAPCReported"),
        "last_position_lat": attributes.get("LastPositionLatitude"),
        "last_position_lng": attributes.get("LastPositionLongitude"),
        "position_reported_at": attributes.get("DateTimePositionReported"),
    }


def _google_candidate_score_for_prediction(prediction: dict, google_candidates: List[dict]) -> int:
    if not google_candidates:
        return 0
    route_name = str(prediction.get("RouteName") or "").strip()
    route_ids = set(_resolve_route_ids(route_name))
    prediction_headsign = prediction.get("Destination") or prediction.get("DirectionText")
    best_score = 0
    for candidate in google_candidates:
        score = 0
        candidate_route = (candidate.get("route_short_name") or "").strip()
        candidate_route_id = (candidate.get("route_id") or "").strip()
        if candidate_route and candidate_route == route_name:
            score += 20
        if candidate_route_id and candidate_route_id in route_ids:
            score += 20
        if prediction_headsign and _headsign_is_compatible(prediction_headsign, candidate.get("trip_headsign")):
            score += 10
        best_score = max(best_score, score)
    return best_score


def _select_route_planning_predictions(
    predictions: List[dict],
    google_candidates: Optional[List[dict]] = None,
    limit: int = ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP,
) -> List[dict]:
    selected: List[dict] = []
    seen: set[str] = set()

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
        if eta > 60:
            continue
        route_name = str(prediction.get("RouteName") or "").strip()
        headsign = str(prediction.get("Destination") or prediction.get("DirectionText") or "").strip()
        direction = str(prediction.get("DirectionId") or "").strip()
        trip_id = str(prediction.get("TripId") or "").strip()
        key = "|".join([route_name, headsign, direction, trip_id])
        if key in seen:
            continue
        seen.add(key)
        selected.append(prediction)
        if len(selected) >= limit:
            break
    return selected


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

    payload = {
        "origin": {"location": {"latLng": {"latitude": start["lat"], "longitude": start["lng"]}}},
        "destination": {"location": {"latLng": {"latitude": end["lat"], "longitude": end["lng"]}}},
        "travelMode": travel_mode,
        "polylineQuality": "HIGH_QUALITY",
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration,routes.legs.steps.polyline.encodedPolyline,routes.legs.steps.navigationInstruction.instructions,routes.legs.steps.localizedValues.distance",
    }
    try:
        data = _post_json("https://routes.googleapis.com/directions/v2:computeRoutes", payload, headers)
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


def _google_transit_candidates(start: dict, end: dict) -> List[dict]:
    if not GOOGLE_MAPS_API:
        return []

    payload = {
        "origin": {"location": {"latLng": {"latitude": start["lat"], "longitude": start["lng"]}}},
        "destination": {"location": {"latLng": {"latitude": end["lat"], "longitude": end["lng"]}}},
        "travelMode": "TRANSIT",
        "departureTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "computeAlternativeRoutes": True,
        "polylineQuality": "HIGH_QUALITY",
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API,
        "X-Goog-FieldMask": "routes.legs.steps.travelMode,routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration,routes.legs.steps.polyline.encodedPolyline,routes.legs.steps.navigationInstruction.instructions,routes.legs.steps.transitDetails.stopDetails,routes.legs.steps.transitDetails.headsign,routes.legs.steps.transitDetails.stopCount,routes.legs.steps.transitDetails.tripShortText,routes.legs.steps.transitDetails.transitLine.name,routes.legs.steps.transitDetails.transitLine.nameShort,routes.legs.steps.transitDetails.transitLine.vehicle.type",
    }
    try:
        data = _post_json("https://routes.googleapis.com/directions/v2:computeRoutes", payload, headers)
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
            route_short_name = (transit_line.get("nameShort") or transit_line.get("name") or "").strip()
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
                    "trip_headsign": (transit_details.get("headsign") or "").strip() or None,
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


def _geocode_destination(destination: str, proximity: Optional[dict] = None) -> dict:
    if not GOOGLE_MAPS_API:
        raise HTTPException(status_code=503, detail="Google Maps API key is not configured")

    payload: dict = {"textQuery": destination, "maxResultCount": 1}
    if proximity:
        payload["locationBias"] = {
            "circle": {
                "center": {"latitude": proximity["lat"], "longitude": proximity["lng"]},
                "radius": 5000.0,
            }
        }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API,
        "X-Goog-FieldMask": "places.location",
    }

    try:
        data = _post_json("https://places.googleapis.com/v1/places:searchText", payload, headers)
        location = data["places"][0]["location"]
        return {"lat": location["latitude"], "lng": location["longitude"]}
    except (requests.RequestException, KeyError, IndexError, TypeError):
        legacy_params: dict = {"query": destination, "key": GOOGLE_MAPS_API}
        if proximity:
            legacy_params["location"] = f'{proximity["lat"]},{proximity["lng"]}'
            legacy_params["radius"] = 5000
        try:
            data = _request_json("https://maps.googleapis.com/maps/api/place/textsearch/json", legacy_params)
            location = data["results"][0]["geometry"]["location"]
            return {"lat": location["lat"], "lng": location["lng"]}
        except (requests.RequestException, KeyError, IndexError, TypeError):
            raise HTTPException(status_code=503, detail="Destination geocoding is unavailable")


def _fetch_nearby_stops(latitude: float, longitude: float, distance_feet: int = 1500) -> tuple[List[dict], str]:
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


def _match_stop_to_gtfs_stop(stop: dict) -> Optional[dict]:
    return _nearest_gtfs_stop(
        {"lat": stop["lat"], "lng": stop["lng"]},
        preferred_name=stop.get("name"),
    )


def _active_gtfs_service_ids(now: Optional[datetime] = None) -> set[str]:
    local_now = _ac_transit_now(now)
    date_key = local_now.strftime("%Y%m%d")
    weekday_key = local_now.strftime("%A").lower()
    active_services: set[str] = set()

    for row in _load_gtfs_calendar():
        service_id = (row.get("service_id") or "").strip()
        if not service_id:
            continue
        if (row.get(weekday_key) or "0").strip() != "1":
            continue
        start_date = (row.get("start_date") or "").strip()
        end_date = (row.get("end_date") or "").strip()
        if start_date and date_key < start_date:
            continue
        if end_date and date_key > end_date:
            continue
        active_services.add(service_id)

    for row in _load_gtfs_calendar_dates().get(date_key, []):
        service_id = (row.get("service_id") or "").strip()
        if not service_id:
            continue
        exception_type = (row.get("exception_type") or "").strip()
        if exception_type == "1":
            active_services.add(service_id)
        elif exception_type == "2" and service_id in active_services:
            active_services.remove(service_id)

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
    gtfs_stop = _match_stop_to_gtfs_stop(stop)
    if not gtfs_stop:
        return []

    local_now = _ac_transit_now(now)
    now_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
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
        service_id = (trip.get("service_id") or "").strip()
        if service_id not in active_service_ids:
            continue
        route_id = (trip.get("route_id") or "").strip()
        route = routes_by_id.get(route_id, {})
        route_name = (route.get("route_short_name") or route_id).strip() or route_id
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

    best_candidate: Optional[dict] = None
    best_score = float("-inf")
    for candidate in google_candidates:
        score = 0.0
        if candidate.get("route_id") and candidate["route_id"] in route_ids:
            score += 8
        if (candidate.get("route_short_name") or "").strip() == route_name:
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
                pass

        if score > best_score:
            best_score = score
            best_candidate = candidate

    return best_candidate if best_score >= 8 else None


def _build_available_buses(
    predictions: List[dict],
    user_position: dict,
    stop: dict,
    destination_marker: dict,
    destination_stop_id: Optional[str],
    google_candidates: List[dict],
) -> List[dict]:
    buses = []
    walk_to_stop = _google_route(user_position, {"lat": stop["lat"], "lng": stop["lng"]})
    matched_boarding_stop = _nearest_gtfs_stop(
        {"lat": stop["lat"], "lng": stop["lng"]},
        preferred_name=stop["name"],
    )
    boarding_stop_id = matched_boarding_stop["stop_id"] if matched_boarding_stop else stop["stop_id"]

    for index, prediction in enumerate(
        sorted(predictions, key=lambda item: _parse_departure(item["PredictedDeparture"]))
    ):
        route_name = prediction.get("RouteName", f"Route {index + 1}")
        delay_seconds = int(prediction.get("PredictedDelayInSeconds", 0) or 0)
        delay_minutes = round(delay_seconds / 60)
        eta = _minutes_until_departure(prediction["PredictedDeparture"])
        if eta > 60:
            continue
        prediction_direction_id = prediction.get("DirectionId")
        if prediction_direction_id is not None:
            try:
                prediction_direction_id = int(prediction_direction_id)
            except (TypeError, ValueError):
                prediction_direction_id = None
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
            transit_segment = None
        if not transit_segment:
            continue

        alighting_stop = transit_segment.get("alighting_stop") if transit_segment else None
        walk_from_origin = (
            {"lat": alighting_stop["lat"], "lng": alighting_stop["lng"]}
            if alighting_stop
            else {"lat": stop["lat"], "lng": stop["lng"]}
        )
        walk_from_stop = _google_route(walk_from_origin, destination_marker)
        route_geometry = transit_segment.get("geometry") if transit_segment else None
        ride_time_minutes = int(transit_segment.get("ride_time_minutes") or 0)
        wait_time_minutes = eta
        total_trip_time_minutes = walk_to_stop["duration_minutes"] + wait_time_minutes + ride_time_minutes + walk_from_stop["duration_minutes"]
        trip_headsign = transit_segment.get("trip_headsign") or prediction.get("Destination") or prediction.get("DirectionText")
        prediction_headsign = prediction.get("Destination") or prediction.get("DirectionText")
        if prediction_headsign and not _headsign_is_compatible(trip_headsign, prediction_headsign):
            continue
        capacity, occupancy = _capacity_for_prediction(prediction, route_name)
        vehicle_attributes = occupancy
        seats_left = None if capacity is None else max(45 - round(45 * capacity / 100), 1)
        bus_position = None
        if vehicle_attributes and vehicle_attributes.get("last_position_lat") is not None:
            bus_position = {
                "lat": vehicle_attributes["last_position_lat"],
                "lng": vehicle_attributes["last_position_lng"],
            }
        fullness = capacity if capacity is not None else occupancy.get("estimated_occupancy_percentage") if occupancy else None

        buses.append(
            {
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
                "trip_id": int(transit_segment["trip_id"]) if transit_segment and str(transit_segment["trip_id"]).isdigit() else prediction.get("TripId"),
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
                "route_geometry": route_geometry,
                "full_route_geometry": transit_segment.get("full_route_geometry"),
                "geometry_source": transit_segment.get("geometry_source"),
                "walk_to_stop": walk_to_stop,
                "walk_from_stop": walk_from_stop,
                "ride_instruction": (google_candidate or {}).get("ride_instruction"),
            }
        )

    return buses


def _datetime_on_service_day(local_date: datetime, seconds_after_midnight: int) -> datetime:
    midnight = datetime.combine(local_date.date(), datetime.min.time(), tzinfo=local_date.tzinfo)
    return midnight + timedelta(seconds=seconds_after_midnight)


def _schedule_commute_preview_for_event(
    event: CalendarEvent,
    origin: Optional[CommuteOrigin],
) -> Optional[CalendarCommutePreview]:
    if not origin or not event.location:
        return None

    try:
        class_start = _ac_transit_now(_parse_departure(event.start_time))
        origin_point = {"lat": origin.lat, "lng": origin.lng}
        destination_point = _geocode_destination(event.location, origin_point)
        destination_stop_id = _nearest_gtfs_stop_id(destination_point)
        active_service_ids = _active_gtfs_service_ids(class_start)
        direct_walk = _google_route(origin_point, destination_point)
    except Exception:
        return CalendarCommutePreview(
            event_id=event.id,
            origin_name=origin.name,
            destination=event.location,
            source="unavailable",
            options=[],
        )

    class_seconds = class_start.hour * 3600 + class_start.minute * 60 + class_start.second
    trips_by_id = _load_gtfs_trips_by_id()
    routes_by_id = _load_gtfs_routes_by_id()
    stop_times_by_stop = _load_gtfs_stop_times_by_stop()
    origin_stops = _nearest_gtfs_stops(origin_point, limit=CALENDAR_COMMUTE_PREVIEW_STOP_LIMIT)
    options: List[CalendarCommuteOption] = []

    direct_leave_by = class_start - timedelta(minutes=direct_walk["duration_minutes"])
    options.append(
        CalendarCommuteOption(
            type="walk",
            leave_by=direct_leave_by.isoformat(),
            depart_at=None,
            arrive_at=class_start.isoformat(),
            total_trip_time_minutes=direct_walk["duration_minutes"],
            walk_time_minutes=direct_walk["duration_minutes"],
        )
    )

    for stop in origin_stops:
        try:
            walk_to_stop = _google_route(origin_point, {"lat": stop["lat"], "lng": stop["lng"]})
        except Exception:
            continue
        latest_boarding_seconds = class_seconds - walk_to_stop["duration_minutes"] * 60
        if latest_boarding_seconds <= 0:
            continue

        candidate_stop_times = [
            stop_time
            for stop_time in stop_times_by_stop.get(stop["stop_id"], [])
            if stop_time.get("departure_seconds") is not None
            and stop_time["departure_seconds"] <= latest_boarding_seconds
        ][-CALENDAR_COMMUTE_PREVIEW_STOP_TIME_LIMIT:]

        for stop_time in candidate_stop_times:
            trip = trips_by_id.get(stop_time["trip_id"])
            if not trip:
                continue
            service_id = (trip.get("service_id") or "").strip()
            if service_id not in active_service_ids:
                continue
            route_id = (trip.get("route_id") or "").strip()
            route = routes_by_id.get(route_id, {})
            route_name = (route.get("route_short_name") or route_id).strip() or route_id
            direction_raw = str(trip.get("direction_id", "")).strip()
            try:
                direction_id = int(direction_raw) if direction_raw else None
            except ValueError:
                direction_id = None

            segment = _build_transit_segment(
                route_name=route_name,
                trip_id=trip.get("trip_id"),
                prediction_direction_id=direction_id,
                boarding_stop_id=stop["stop_id"],
                destination_stop_id=destination_stop_id,
                destination_point=destination_point,
                google_candidate=None,
            )
            if not segment:
                continue

            alighting_stop = segment.get("alighting_stop") or {}
            try:
                walk_from_stop = _google_route(
                    {"lat": alighting_stop["lat"], "lng": alighting_stop["lng"]},
                    destination_point,
                )
            except Exception:
                continue

            departure_seconds = stop_time["departure_seconds"]
            depart_at = _datetime_on_service_day(class_start, departure_seconds)
            ride_minutes = int(segment.get("ride_time_minutes") or 0)
            arrive_at = depart_at + timedelta(minutes=ride_minutes + walk_from_stop["duration_minutes"])
            if arrive_at > class_start:
                continue

            leave_by = depart_at - timedelta(minutes=walk_to_stop["duration_minutes"])
            if leave_by <= _ac_transit_now():
                continue

            total_trip_minutes = max(round((arrive_at - leave_by).total_seconds() / 60), 1)
            options.append(
                CalendarCommuteOption(
                    type="bus",
                    route_number=route_name,
                    route_name=segment.get("route_long_name") or f"Route {route_name}",
                    leave_by=leave_by.isoformat(),
                    depart_at=depart_at.isoformat(),
                    arrive_at=arrive_at.isoformat(),
                    total_trip_time_minutes=total_trip_minutes,
                    walk_time_minutes=walk_to_stop["duration_minutes"],
                    ride_time_minutes=ride_minutes,
                    destination_walk_time_minutes=walk_from_stop["duration_minutes"],
                    boarding_stop_name=stop["stop_name"],
                    alighting_stop_name=alighting_stop.get("stop_name"),
                )
            )

    deduped: Dict[str, CalendarCommuteOption] = {}
    for option in options:
        key = "|".join([
            option.type,
            option.route_number or "walk",
            option.boarding_stop_name or "",
            option.alighting_stop_name or "",
        ])
        existing = deduped.get(key)
        if existing is None or option.leave_by > existing.leave_by:
            deduped[key] = option

    sorted_options = sorted(
        deduped.values(),
        key=lambda option: (option.leave_by, -option.total_trip_time_minutes),
        reverse=True,
    )[:5]
    best_option = sorted_options[0] if sorted_options else None
    return CalendarCommutePreview(
        event_id=event.id,
        origin_name=origin.name,
        destination=event.location,
        source="schedule" if sorted_options else "unavailable",
        leave_by=best_option.leave_by if best_option else None,
        best_route_number=best_option.route_number if best_option and best_option.type == "bus" else None,
        options=sorted_options,
    )


def _calendar_commute_previews(
    events: List[CalendarEvent],
    origin: Optional[CommuteOrigin],
) -> Optional[List[CalendarCommutePreview]]:
    if not origin:
        return None
    upcoming_events = sorted(
        [event for event in events if _is_future_event(event.start_time)],
        key=lambda event: event.start_time,
    )[:CALENDAR_COMMUTE_PREVIEW_EVENT_LIMIT]
    previews = [
        preview
        for event in upcoming_events
        if (preview := _schedule_commute_preview_for_event(event, origin)) is not None
    ]
    return previews or None


def _fetch_predictions(stop_id: str) -> tuple[List[dict], str]:
    if not ACTRANSIT_API_KEY:
        return [], "unavailable"

    url = f"https://api.actransit.org/transit/stops/{stop_id}/predictions?token={ACTRANSIT_API_KEY}"
    try:
        response = requests.get(url, timeout=6)
        response.raise_for_status()
        data = response.json() or []
        live_predictions = [
            prediction
            for prediction in data
            if _prediction_is_upcoming(prediction)
        ]
        return live_predictions, "live"
    except requests.RequestException:
        return [], "unavailable"


@app.get("/")
def root():
    return {"message": "NextStop backend is running", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/gtfs")
def debug_gtfs():
    import time
    results = {}
    for name, fn in [
        ("trips", _load_gtfs_trips),
        ("routes", _load_gtfs_routes),
        ("stops", _load_gtfs_stops_by_id),
        ("stop_times_by_trip", _load_gtfs_stop_times_by_trip),
        ("stop_times_by_stop", _load_gtfs_stop_times_by_stop),
        ("calendar", _load_gtfs_calendar),
    ]:
        t = time.time()
        try:
            data = fn()
            results[name] = {"status": "ok", "count": len(data), "ms": int((time.time() - t) * 1000)}
        except Exception as e:
            results[name] = {"status": "error", "detail": str(e), "ms": int((time.time() - t) * 1000)}
    return results


@app.get("/debug/actransit")
def debug_actransit():
    if not ACTRANSIT_API_KEY:
        return {"status": "error", "detail": "ACTRANSIT_API_KEY not set"}
    import time
    # Test 1: nearby stops around UC Berkeley
    t0 = time.time()
    try:
        stops, source = _fetch_nearby_stops(37.8719, -122.2585)
        stops_ms = int((time.time() - t0) * 1000)
        stop_result = {"status": "ok", "count": len(stops), "ms": stops_ms}
    except Exception as e:
        stop_result = {"status": "error", "detail": str(e), "ms": int((time.time() - t0) * 1000)}
    # Test 2: predictions for stop 51303 (Telegraph & Bancroft — common AC Transit stop)
    t1 = time.time()
    try:
        preds, _ = _fetch_predictions("51303")
        preds_ms = int((time.time() - t1) * 1000)
        pred_result = {"status": "ok", "count": len(preds), "ms": preds_ms}
    except Exception as e:
        pred_result = {"status": "error", "detail": str(e), "ms": int((time.time() - t1) * 1000)}
    return {"nearby_stops": stop_result, "predictions": pred_result}


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
    """Invalidate the current session token."""
    if not x_session_token:
        raise HTTPException(status_code=401, detail="No session token provided")
    
    with _db() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token = ?",
            (x_session_token,),
        )
    
    return {"status": "success", "message": "Logged out successfully"}


class RefreshTokenRequest(BaseModel):
    token: str


@app.post("/auth/refresh", response_model=AuthResponse)
def refresh_token(data: RefreshTokenRequest):
    """Refresh an expiring session token."""
    user = _user_from_token(data.token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please login again.")
    
    # Invalidate old token
    with _db() as connection:
        connection.execute(
            "DELETE FROM sessions WHERE token = ?",
            (data.token,),
        )
    
    # Create new token
    new_token = _create_session(user["id"])
    return AuthResponse(token=new_token, email=user["email"], name=user["name"])


@app.post("/telemetry")
def receive_telemetry(data: Telemetry):
    record = data.dict()
    if record["timestamp"] is None:
        record["timestamp"] = datetime.utcnow().isoformat()

    latest_bus_metrics[data.bus_id] = record
    return {"status": "success", "stored_bus_id": data.bus_id}


@app.get("/bus/{bus_id}")
def get_bus(bus_id: str):
    if bus_id not in latest_bus_metrics:
        raise HTTPException(status_code=404, detail="Bus not found")

    return latest_bus_metrics[bus_id]


@app.get("/next-trip/{stop_id}")
def get_next_trip(stop_id: str):
    next_bus_response = get_next_bus(stop_id)
    next_bus = next_bus_response["next_bus"]

    if next_bus is None:
        return {
            "stop_id": stop_id,
            "next_bus": None,
            "recommendation": "No buses available right now"
        }

    return {
        "stop_id": stop_id,
        "next_bus": next_bus,
        "recommendation": f"Leave now to catch Route {next_bus['RouteName']}"
    }


@app.get("/test-actransit")
def test_actransit():
    data, source = _fetch_predictions("54963")

    if not data:
        return {"status_code": 200, "source": source, "next_bus": None}

    next_bus = sorted(
        data,
        key=lambda item: _parse_departure(item["PredictedDeparture"]),
    )[0]

    return {
        "status_code": 200,
        "source": source,
        "next_bus": next_bus
    }


@app.get("/next-bus/{stop_id}")
def get_next_bus(stop_id: str):
    data, source = _fetch_predictions(stop_id)

    if not data:
        return {
            "status_code": 200,
            "source": source,
            "stop_id": stop_id,
            "next_bus": None
        }

    next_bus = sorted(
        data,
        key=lambda item: _parse_departure(item["PredictedDeparture"]),
    )[0]

    return {
        "status_code": 200,
        "source": source,
        "stop_id": stop_id,
        "next_bus": next_bus
    }


def _unfold_ics(content: str) -> List[str]:
    unfolded: List[str] = []
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _clean_ics_text(value: str) -> str:
    return (
        value
        .replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .strip()
    )


def _parse_ics_params(key: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for part in key.split(";")[1:]:
        if "=" not in part:
            continue
        param_key, param_value = part.split("=", 1)
        params[param_key.upper()] = param_value
    return params


def _parse_ics_datetime(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith("Z"):
        parsed = datetime.strptime(normalized, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    if "T" in normalized:
        parsed = datetime.strptime(normalized[:15], "%Y%m%dT%H%M%S")
        return parsed.isoformat()
    parsed = datetime.strptime(normalized[:8], "%Y%m%d")
    return parsed.isoformat()


def _parse_ics_datetime_object(value: str) -> datetime:
    if "-" in value:
        return datetime.fromisoformat(value)
    return datetime.fromisoformat(_parse_ics_datetime(value))


def _parse_rrule(value: str) -> Dict[str, str]:
    rule: Dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, rule_value = part.split("=", 1)
        rule[key.upper()] = rule_value
    return rule


def _align_datetime_for_compare(value: datetime, reference: datetime) -> datetime:
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _event_duration(event: dict) -> timedelta:
    try:
        start = _parse_ics_datetime_object(event["start_time"])
        end = _parse_ics_datetime_object(event.get("end_time") or event["start_time"])
    except (KeyError, ValueError):
        return timedelta(hours=1)
    duration = end - start
    return duration if duration.total_seconds() > 0 else timedelta(hours=1)


def _serialize_ics_datetime(value: datetime) -> str:
    return value.isoformat()


def _expand_recurring_event(event: dict) -> List[dict]:
    rrule = event.get("rrule")
    if not rrule:
        return [event]

    rule = _parse_rrule(rrule)
    if rule.get("FREQ") != "WEEKLY":
        return [event]

    try:
        base_start = _parse_ics_datetime_object(event["start_time"])
    except (KeyError, ValueError):
        return [event]

    duration = _event_duration(event)
    now = datetime.now(base_start.tzinfo) if base_start.tzinfo else datetime.now()
    until_raw = rule.get("UNTIL")
    count_raw = rule.get("COUNT")
    try:
        interval = max(int(rule.get("INTERVAL", "1") or "1"), 1)
    except ValueError:
        interval = 1
    until = None
    if until_raw:
        try:
            until = _align_datetime_for_compare(_parse_ics_datetime_object(until_raw), base_start)
        except ValueError:
            until = None

    byday = {
        "MO": 0,
        "TU": 1,
        "WE": 2,
        "TH": 3,
        "FR": 4,
        "SA": 5,
        "SU": 6,
    }
    target_weekdays = [
        byday[item]
        for item in (rule.get("BYDAY") or "").split(",")
        if item in byday
    ] or [base_start.weekday()]

    max_count = int(count_raw) if count_raw and count_raw.isdigit() else 260
    horizon = now + timedelta(days=210)
    occurrences: List[dict] = []
    base_week_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=base_start.weekday())
    cursor_week_start = base_week_start
    excluded_starts = set(event.get("exdate") or [])
    generated = 0

    while generated < max_count and cursor_week_start <= horizon:
        week_delta = (cursor_week_start - base_week_start).days // 7
        if week_delta % interval != 0:
            cursor_week_start += timedelta(days=7)
            continue

        for weekday in target_weekdays:
            occurrence_start = cursor_week_start + timedelta(
                days=weekday,
                hours=base_start.hour,
                minutes=base_start.minute,
                seconds=base_start.second,
                microseconds=base_start.microsecond,
            )
            if occurrence_start < base_start:
                continue
            if until and occurrence_start > until:
                continue
            if occurrence_start.isoformat() in excluded_starts:
                continue
            generated += 1
            if occurrence_start + duration < now:
                continue
            occurrence = dict(event)
            occurrence["start_time"] = _serialize_ics_datetime(occurrence_start)
            occurrence["end_time"] = _serialize_ics_datetime(occurrence_start + duration)
            occurrence["uid"] = f"{event.get('uid') or event.get('summary')}-{occurrence_start.isoformat()}"
            occurrences.append(occurrence)
            if len(occurrences) >= 80:
                return occurrences

        cursor_week_start += timedelta(days=7)

    return occurrences or [event]


def _parse_ics_events(content: str) -> List[CalendarEvent]:
    events: List[CalendarEvent] = []
    current: Optional[dict] = None

    for raw_line in _unfold_ics(content):
        if raw_line == "BEGIN:VEVENT":
            current = {}
            continue
        if raw_line == "END:VEVENT":
            if current and not current.get("invalid_datetime") and current.get("summary") and current.get("start_time"):
                for occurrence in _expand_recurring_event(current):
                    events.append(
                        CalendarEvent(
                            id=occurrence.get("uid") or f"calendar_{len(events)}",
                            title=occurrence["summary"],
                            location=occurrence.get("location", ""),
                            start_time=occurrence["start_time"],
                            end_time=occurrence.get("end_time"),
                        )
                    )
            current = None
            continue
        if current is None or ":" not in raw_line:
            continue

        key, value = raw_line.split(":", 1)
        field = key.split(";", 1)[0].upper()
        params = _parse_ics_params(key)

        if field == "UID":
            current["uid"] = _clean_ics_text(value)
        elif field == "SUMMARY":
            current["summary"] = _clean_ics_text(value)
        elif field == "LOCATION":
            current["location"] = _clean_ics_text(value)
        elif field == "DTSTART":
            try:
                current["start_time"] = _parse_ics_datetime(value)
            except ValueError:
                current["invalid_datetime"] = True
        elif field == "DTEND":
            try:
                current["end_time"] = _parse_ics_datetime(value)
            except ValueError:
                current["end_time"] = None
        elif field == "RRULE":
            current["rrule"] = value.strip()
        elif field == "DURATION" and not current.get("end_time"):
            pass
        elif field == "RECURRENCE-ID":
            try:
                current["recurrence_id"] = _parse_ics_datetime(value)
            except ValueError:
                current["recurrence_id"] = None
        elif field == "EXDATE":
            for item in value.split(","):
                if not item.strip():
                    continue
                try:
                    current.setdefault("exdate", []).append(_parse_ics_datetime(item))
                except ValueError:
                    continue

    return events


def _infer_course_code(title: str) -> str:
    match = re.search(r"\b([A-Z]{2,}(?:\s+[A-Z]{1,4})?)\s*[-:]?\s*([A-Z]?\d{1,4}[A-Z]?)\b", title.upper())
    if match:
        subject = re.sub(r"\s+", " ", match.group(1)).strip()
        return f"{subject} {match.group(2)}"
    return title[:12] or "CLASS"


def _extract_course_code(title: str) -> Optional[str]:
    normalized = title.upper().replace("\\", " ")
    known_courses = ["ENGIN 183C", "CDSS 170", "DATA 145"]
    compact_title = re.sub(r"[^A-Z0-9]", "", normalized)
    for course in known_courses:
        if re.sub(r"[^A-Z0-9]", "", course) in compact_title:
            return course
    match = re.search(r"\b([A-Z]{2,}(?:\s+[A-Z]{1,4})?)\s*[-:]?\s*([A-Z]?\d{1,4}[A-Z]?)\b", normalized)
    if not match:
        return None
    subject = re.sub(r"\s+", " ", match.group(1)).strip()
    return f"{subject} {match.group(2)}"


def _is_future_event(start_time: str) -> bool:
    try:
        parsed = datetime.fromisoformat(start_time)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return parsed > datetime.now()
    return parsed > datetime.now(timezone.utc)


def _filter_and_annotate_calendar_events(events: List[CalendarEvent]) -> List[CalendarEvent]:
    filtered: List[CalendarEvent] = []
    for event in events:
        location = (event.location or "").strip()
        if not location:
            continue
        if not _is_future_event(event.start_time):
            continue
        course_code = _extract_course_code(event.title)
        is_course = course_code is not None
        filtered.append(
            CalendarEvent(
                id=event.id,
                title=event.title,
                location=location,
                start_time=event.start_time,
                end_time=event.end_time,
                is_course=is_course,
                course_code=course_code,
            )
        )
    return filtered


def _store_calendar_events(user_id: int, events: List[CalendarEvent], source: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.execute("DELETE FROM calendar_events WHERE user_id = ? AND source != 'manual'", (user_id,))
        connection.executemany(
            """
            INSERT INTO calendar_events
            (user_id, external_id, title, location, start_time, end_time, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (user_id, event.id, event.title, event.location, event.start_time, event.end_time, source, now)
                for event in events
            ],
        )


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
        event = CalendarEvent(
            id=row["external_id"] or str(index),
            title=row["title"],
            location=row["location"] or "",
            start_time=row["start_time"],
            end_time=row["end_time"],
        )
        if row["source"] == "manual":
            manual_events.append(event)
        else:
            ics_events.append(event)
    filtered_ics = _filter_and_annotate_calendar_events(ics_events)
    # Manual events are always courses; skip location filter since it's optional
    filtered_manual = [
        CalendarEvent(
            id=event.id,
            title=event.title,
            location=event.location,
            start_time=event.start_time,
            end_time=event.end_time,
            is_course=True,
            course_code=_extract_course_code(event.title),
        )
        for event in manual_events
        if _is_future_event(event.start_time)
    ]
    all_events = sorted(filtered_ics + filtered_manual, key=lambda e: e.start_time)
    source = rows[0]["source"] if rows else "sqlite"
    return CalendarImportResponse(source=source, events=all_events)


@app.post("/calendar/import", response_model=CalendarImportResponse)
async def import_calendar(
    file: UploadFile = File(...),
    commute_origin: Optional[str] = Form(default=None),
    x_session_token: Optional[str] = Header(default=None),
):
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
    parsed_origin: Optional[CommuteOrigin] = None
    if commute_origin:
        try:
            parsed_origin = CommuteOrigin(**json.loads(commute_origin))
        except (TypeError, ValueError):
            parsed_origin = None
    return CalendarImportResponse(
        source="ics",
        events=events,
        commute_previews=_calendar_commute_previews(events, parsed_origin),
    )


class CalendarEventUpdateItem(BaseModel):
    external_ids: List[str]
    title: Optional[str] = None
    location: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class BulkCalendarEventUpdate(BaseModel):
    updates: List[CalendarEventUpdateItem]


@app.patch("/calendar/events")
def update_calendar_events(
    request: BulkCalendarEventUpdate,
    x_session_token: Optional[str] = Header(default=None),
):
    user = _require_user(x_session_token)
    with _db() as connection:
        for item in request.updates:
            if not item.external_ids:
                continue
            set_clauses: List[str] = []
            params: List = []
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
            connection.execute(
                f"UPDATE calendar_events SET {', '.join(set_clauses)} WHERE external_id IN ({placeholders}) AND user_id = ?",
                params,
            )
    return {"status": "ok"}


class DeleteEventsRequest(BaseModel):
    external_ids: List[str]


@app.delete("/calendar/events")
def delete_calendar_events(
    request: DeleteEventsRequest,
    x_session_token: Optional[str] = Header(default=None),
):
    user = _require_user(x_session_token)
    with _db() as connection:
        placeholders = ",".join("?" for _ in request.external_ids)
        connection.execute(
            f"DELETE FROM calendar_events WHERE external_id IN ({placeholders}) AND user_id = ?",
            [*request.external_ids, user["id"]],
        )
    return {"status": "ok"}


class ManualEventsRequest(BaseModel):
    events: List[CalendarEvent]
    commute_origin: Optional[CommuteOrigin] = None


@app.post("/calendar/events", response_model=CalendarImportResponse)
def add_manual_calendar_events(
    request: ManualEventsRequest,
    x_session_token: Optional[str] = Header(default=None),
):
    user = _require_user(x_session_token)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.executemany(
            """
            INSERT INTO calendar_events
            (user_id, external_id, title, location, start_time, end_time, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (user["id"], event.id, event.title, event.location, event.start_time, event.end_time, "manual", now)
                for event in request.events
            ],
        )
    return CalendarImportResponse(
        source="manual",
        events=request.events,
        commute_previews=_calendar_commute_previews(request.events, request.commute_origin),
    )


@app.get("/locations/search")
def search_locations(q: str, lat: Optional[float] = None, lng: Optional[float] = None):
    if not GOOGLE_MAPS_API:
        raise HTTPException(status_code=503, detail="Google Maps API key is not configured")
    payload: dict = {"textQuery": q, "maxResultCount": 6}
    if lat is not None and lng is not None:
        payload["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 5000.0,
            }
        }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location",
    }
    try:
        data = _post_json("https://places.googleapis.com/v1/places:searchText", payload, headers)
    except requests.RequestException:
        legacy_params: dict = {"query": q, "key": GOOGLE_MAPS_API}
        if lat is not None and lng is not None:
            legacy_params["location"] = f"{lat},{lng}"
            legacy_params["radius"] = 5000
        try:
            legacy_data = _request_json("https://maps.googleapis.com/maps/api/place/textsearch/json", legacy_params)
            return {
                "results": [
                    {
                        "id": place.get("place_id"),
                        "name": place.get("name") or place.get("formatted_address"),
                        "address": place.get("formatted_address"),
                        "lat": place["geometry"]["location"]["lat"],
                        "lng": place["geometry"]["location"]["lng"],
                    }
                    for place in legacy_data.get("results", [])
                ]
            }
        except requests.RequestException:
            raise HTTPException(status_code=503, detail="Google Places search is unavailable")
    return {
        "results": [
            {
                "id": place.get("id"),
                "name": place.get("displayName", {}).get("text") or place.get("formattedAddress"),
                "address": place.get("formattedAddress"),
                "lat": place["location"]["latitude"],
                "lng": place["location"]["longitude"],
            }
            for place in data.get("places", [])
        ]
    }


@app.get("/locations/reverse")
def reverse_location(lat: float, lng: float):
    fallback_address = f"{lat:.4f}, {lng:.4f}"
    if not GOOGLE_MAPS_API:
        return {"name": fallback_address, "address": fallback_address}

    try:
        data = _request_json(
            "https://maps.googleapis.com/maps/api/geocode/json",
            {"latlng": f"{lat},{lng}", "key": GOOGLE_MAPS_API},
        )
        result = (data.get("results") or [None])[0]
        if not result:
            return {"name": fallback_address, "address": fallback_address}
        address = result.get("formatted_address") or fallback_address
        address_components = result.get("address_components") or []
        concise_name = None
        for component in address_components:
            types = component.get("types") or []
            if any(item in types for item in ["premise", "point_of_interest", "establishment", "subpremise"]):
                concise_name = component.get("long_name")
                break
        if not concise_name:
            concise_name = address.split(",")[0].strip() if address else fallback_address
        return {"name": concise_name, "address": address}
    except requests.RequestException:
        return {"name": fallback_address, "address": fallback_address}


@app.get("/service-alert")
def service_alert(routes: Optional[str] = None):
    route_list = [route.strip() for route in (routes or "").split(",") if route.strip()]
    alert = _fetch_service_alert(route_list)
    return {"alert": alert}


@app.get("/config/maps")
def maps_config():
    return {"googleMapsApiKey": GOOGLE_MAPS_API}


@app.get("/gtfs/shape")
def get_gtfs_shape(route_id: str, direction_id: Optional[int] = None):
    coordinates = _gtfs_route_shape_coordinates(route_id, direction_id=direction_id)
    if not coordinates:
        raise HTTPException(
            status_code=404,
            detail=f"No GTFS shape found for route_id={route_id}"
            + ("" if direction_id is None else f", direction_id={direction_id}"),
        )
    return coordinates


@app.get("/route/vehicles")
def get_route_vehicles(route_id: str, direction_id: Optional[int] = None, boarding_stop_id: Optional[str] = None):
    vehicles = _fetch_route_vehicles(route_id, direction_id=direction_id)
    trips_by_id = _load_gtfs_trips_by_id()
    prediction_by_vehicle: Dict[str, dict] = {}
    if boarding_stop_id:
        predictions, _ = _fetch_predictions(boarding_stop_id)
        for prediction in predictions:
            vehicle_id = prediction.get("VehicleId")
            if vehicle_id is None:
                continue
            prediction_by_vehicle[str(vehicle_id)] = prediction

    return [
        {
            "vehicle_id": vehicle.get("VehicleId"),
            "trip_id": vehicle.get("CurrentTripId"),
            "lat": vehicle.get("Latitude"),
            "lng": vehicle.get("Longitude"),
            "heading": vehicle.get("Heading"),
            "reported_at": vehicle.get("TimeLastReported"),
            "arrival_minutes": (
                _minutes_until_departure(prediction_by_vehicle[str(vehicle.get("VehicleId"))]["PredictedDeparture"])
                if vehicle.get("VehicleId") is not None and str(vehicle.get("VehicleId")) in prediction_by_vehicle
                else None
            ),
            "headsign": (
                (
                    prediction_by_vehicle[str(vehicle.get("VehicleId"))].get("Destination")
                    or prediction_by_vehicle[str(vehicle.get("VehicleId"))].get("DirectionText")
                )
                if vehicle.get("VehicleId") is not None and str(vehicle.get("VehicleId")) in prediction_by_vehicle
                else (trips_by_id.get(str(vehicle.get("CurrentTripId"))) or {}).get("trip_headsign")
            ),
            "occupancy_percent": _vehicle_occupancy_for_marker(
                str(vehicle.get("VehicleId")) if vehicle.get("VehicleId") is not None else None,
                route_id,
            ),
        }
        for vehicle in vehicles
        if vehicle.get("Latitude") is not None and vehicle.get("Longitude") is not None
    ]


@app.get("/plan-trip")
def plan_trip(
    destination: str,
    start: str = "Current location",
    start_lat: Optional[float] = None,
    start_lng: Optional[float] = None,
    destination_lat: Optional[float] = None,
    destination_lng: Optional[float] = None,
):
    if start_lat is None or start_lng is None:
        raise HTTPException(status_code=400, detail="start_lat and start_lng are required")

    start_marker = {"lat": start_lat, "lng": start_lng}
    destination_marker = (
        {"lat": destination_lat, "lng": destination_lng}
        if destination_lat is not None and destination_lng is not None
        else _geocode_destination(destination, start_marker)
    )
    google_candidates = _google_transit_candidates(start_marker, destination_marker)

    nearby_stops, stop_source = _fetch_nearby_stops(start_marker["lat"], start_marker["lng"])
    normalized_stops = []
    for stop in nearby_stops[:8]:
        try:
            normalized_stops.append(_normalize_stop(stop))
        except (TypeError, ValueError):
            continue

    candidate_stops = normalized_stops[:]
    seen_candidate_stop_ids = {stop["stop_id"] for stop in candidate_stops}
    for candidate in google_candidates:
        boarding_stop_id = candidate.get("boarding_stop_id")
        boarding_point = candidate.get("boarding_stop_point") or {}
        if not boarding_stop_id or boarding_stop_id in seen_candidate_stop_ids:
            continue
        if boarding_point.get("lat") is None or boarding_point.get("lng") is None:
            continue
        candidate_stops.append(
            {
                "stop_id": str(boarding_stop_id),
                "name": candidate.get("boarding_stop_name") or "Selected boarding stop",
                "lat": boarding_point["lat"],
                "lng": boarding_point["lng"],
                "city": None,
            }
        )
        seen_candidate_stop_ids.add(str(boarding_stop_id))

    origin_stop = candidate_stops[0] if candidate_stops else None
    start_stop_id = origin_stop["stop_id"] if origin_stop else None
    destination_stop_id = _nearest_gtfs_stop_id(destination_marker)
    direct_walk = _google_route(start_marker, destination_marker)
    prediction_sources: List[str] = []
    available_buses: List[dict] = []

    def _fetch_stop_buses(stop: dict):
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

    google_boarding_stop_ids = {
        str(candidate.get("boarding_stop_id"))
        for candidate in google_candidates
        if candidate.get("boarding_stop_id")
    }
    route_planning_stops: List[dict] = []
    route_planning_stop_ids: set[str] = set()
    for stop in candidate_stops:
        if stop["stop_id"] not in google_boarding_stop_ids:
            continue
        route_planning_stops.append(stop)
        route_planning_stop_ids.add(stop["stop_id"])
        if len(route_planning_stops) >= ROUTE_PLAN_MAX_STOPS:
            break
    for stop in candidate_stops:
        if len(route_planning_stops) >= ROUTE_PLAN_MAX_STOPS:
            break
        if stop["stop_id"] in route_planning_stop_ids:
            continue
        route_planning_stops.append(stop)
        route_planning_stop_ids.add(stop["stop_id"])
    max_workers = max(1, min(ROUTE_PLAN_MAX_STOPS, len(route_planning_stops)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_stop_buses, stop): stop for stop in route_planning_stops}
        for future in as_completed(futures):
            try:
                prediction_source, buses = future.result()
                prediction_sources.append(prediction_source)
                available_buses.extend(buses)
            except Exception:
                pass

    deduped_buses: Dict[str, dict] = {}
    for bus in available_buses:
        key = "|".join(
            [
                str(bus.get("route_id") or bus.get("route_short_name") or bus.get("route_number")),
                str((bus.get("boarding_stop") or {}).get("stop_id") or ""),
                str((bus.get("alighting_stop") or {}).get("stop_id") or ""),
            ]
        )
        existing = deduped_buses.get(key)
        if existing is None or bus.get("eta", 10**9) < existing.get("eta", 10**9):
            deduped_buses[key] = bus

    direct_walk_minutes = direct_walk.get("duration_minutes")
    available_buses = sorted(
        (
            bus
            for bus in deduped_buses.values()
            if (
                direct_walk_minutes is None
                or bus.get("total_trip_time_minutes", 10**9) <= direct_walk_minutes
                or (
                    int(bus.get("walk_time") or 0)
                    + int(bus.get("destination_walk_time") or 0)
                ) < direct_walk_minutes
            )
        ),
        key=lambda bus: (
            bus.get("total_trip_time_minutes", 10**9),
            bus.get("eta", 10**9),
        ),
    )[:ROUTE_PLAN_MAX_TOTAL_OPTIONS]

    if prediction_sources and any(item == "live" for item in prediction_sources):
        source = "live"
    elif prediction_sources and any(item == "schedule" for item in prediction_sources):
        source = "schedule"
    else:
        source = "unavailable"
    next_bus = available_buses[0] if available_buses else None

    if next_bus is None:
        recommendation = "No buses available right now"
    else:
        recommendation = (
            f"Walk to {next_bus['current_stop']} for Route {next_bus['route_number']} "
            f"toward {next_bus.get('trip_headsign') or destination}."
        )

    return {
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
        "direct_walk": direct_walk,
    }


@app.get("/config/maps")
def get_maps_config():
    print("GOOGLE_MAPS_API loaded:", bool(GOOGLE_MAPS_API))
    return {"googleMapsApiKey": GOOGLE_MAPS_API}
