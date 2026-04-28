import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _resolve_path(env_var: str, default: str) -> str:
    raw = os.getenv(env_var, default)
    if os.path.isabs(raw):
        return raw
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), raw)


ACTRANSIT_API_KEY = os.getenv("ACTRANSIT_API_KEY")
GOOGLE_MAPS_API = os.getenv("GOOGLE_MAPS_API")
AC_TRANSIT_TIMEZONE = ZoneInfo("America/Los_Angeles")
DATABASE_PATH = _resolve_path("DATABASE_PATH", "nextstop.db")
GTFS_STATIC_DIR = _resolve_path("GTFS_STATIC_DIR", "gtfs")

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "capacitor://localhost",
    "ionic://localhost",
    "http://localhost",
    "https://localhost",
]

CONFIGURED_CORS_ORIGINS = [
    stripped
    for raw in os.getenv("CORS_ORIGINS", "").split(",")
    if (stripped := raw.strip())
]

ALLOWED_CORS_ORIGINS = sorted(set(DEFAULT_CORS_ORIGINS + CONFIGURED_CORS_ORIGINS))

# --- tunables ---
VEHICLE_ATTRIBUTES_TTL_SECONDS = 90
ROUTE_PLAN_MAX_STOPS = 4
ROUTE_PLAN_MAX_PREDICTIONS_PER_STOP = 2
ROUTE_PLAN_MAX_TOTAL_OPTIONS = 8
CALENDAR_COMMUTE_PREVIEW_EVENT_LIMIT = 3
CALENDAR_COMMUTE_PREVIEW_STOP_LIMIT = 2
CALENDAR_COMMUTE_PREVIEW_STOP_TIME_LIMIT = 4
MAX_TELEMETRY_ENTRIES = 5000

# --- session / auth ---
SESSION_EXPIRY_HOURS = 24
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_MINUTES = 15

# --- network / API ---
HTTP_TIMEOUT_SECONDS = 6
GOOGLE_PLACES_SEARCH_RADIUS_METERS = 5000

# --- routing / scoring ---
ETA_THRESHOLD_MINUTES = 60
WALK_SPEED_METERS_PER_MINUTE = 80
ASSUMED_BUS_SEAT_CAPACITY = 45
METERS_PER_DEGREE_LAT = 111_320
NEARBY_STOPS_RADIUS_FEET = 1500
MAX_GTFS_STOP_DISTANCE_METERS = 1800

# --- product metrics ---
UC_BERKELEY_CENTER = {"lat": 37.8719, "lng": -122.2585}
CO2_GRAMS_PER_KM_CAR = 251
DEFAULT_USER_PREFERENCES = {
    "smart_departure_alerts": True,
    "capacity_alerts": True,
    "delay_notifications": True,
    "theme_id": None,
}
