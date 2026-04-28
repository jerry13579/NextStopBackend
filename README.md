# NextStop — Backend

FastAPI service providing live transit routing, schedule-based commute previews, and crowd telemetry for the NextStop iOS app. Deployed on Azure.

## What it does

- **Route planning** (`GET /plan-trip`) — given a user's GPS coordinates and a destination string, finds nearby AC Transit stops, fetches live predictions, builds transit segments from local GTFS data, and returns ranked bus options with walking legs from Google Routes.
- **Calendar commute previews** (`POST /calendar/import`, `POST /calendar/events`) — parses `.ics` files or manual class entries, computes scheduled departure options for each upcoming class using GTFS stop times and Google Transit candidates for the class's departure time.
- **Live vehicle positions** (`GET /route/vehicles`) — returns real-time bus positions and ETA to the boarding stop for a given route and direction.
- **Crowd telemetry** (`POST /telemetry`) — stores occupancy reports from the app; capacity is `null` in responses when no telemetry has been received.
- **Stop predictions** (`GET /next-bus/{stop_id}`) — live departure predictions for a single stop.
- **Service alerts** (`GET /service-alert`) — active AC Transit alerts for a given set of route numbers.
- **Geocoding / location search** (`GET /locations/search`, `GET /locations/reverse`) — powered by Google Places.
- **Google Maps key delivery** (`GET /config/maps`) — serves the Maps JS API key to the frontend so it never ships in the app bundle.
- **Auth** (`POST /auth/register`, `POST /auth/login`, `POST /auth/session`, `POST /auth/logout`) — lightweight email/password auth backed by SQLite.

## Data sources

| Source | Used for |
|---|---|
| AC Transit REST API | Live stop predictions, nearby stop lookup |
| Google Routes API | Walking directions, transit candidate routes |
| Google Places API | Destination geocoding, location search |
| AC Transit GTFS | Stop times, trip schedules, route shapes, service calendars |

GTFS files are downloaded and cached at startup from the AC Transit GTFS feed. The app will not start without them.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
uvicorn main:app --reload
```

The server starts on `http://127.0.0.1:8000`. Check `/health` and `/docs`.

**.env keys:**

```
ACTRANSIT_API_KEY=       # AC Transit developer portal
GOOGLE_MAPS_API=         # Google Cloud — Routes, Places, Maps JS APIs enabled
CORS_ORIGINS=            # Optional comma-separated extra origins
SECRET_KEY=              # Random string used to sign session tokens
```

Route planning degrades gracefully without API keys (returns `source: "unavailable"`), but the app is not useful without them.

## Azure deployment

The backend is deployed as an Azure App Service (Python runtime).

**App Service settings:**

| Setting | Value |
|---|---|
| Runtime | Python 3.11 |
| Startup command | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| Health check path | `/health` |

**Required environment variables** (set in App Service → Configuration → Application settings):

```
ACTRANSIT_API_KEY
GOOGLE_MAPS_API
SECRET_KEY
```

`CORS_ORIGINS` is optional. The backend already allows local development and Capacitor/iOS origins by default; set it only when adding a new deployed web frontend origin.

After deploying, verify:

```bash
curl https://<your-app>.azurewebsites.net/health
curl "https://<your-app>.azurewebsites.net/plan-trip?destination=Soda+Hall&start_lat=37.8719&start_lng=-122.2585"
```

## Key implementation notes

- **GTFS loading** — all GTFS files are loaded into memory with `@lru_cache` at startup. The first request after a cold start is slow; subsequent requests are fast.
- **Schedule vs live** — `/plan-trip` tries AC Transit live predictions first and falls back to GTFS scheduled times. The response includes a `source` field: `"live"`, `"schedule"`, or `"unavailable"`.
- **Commute previews** — `_schedule_commute_preview_for_event` calls `_google_transit_candidates` with the class's actual start time as the departure time, so Google returns routes that run at that future time rather than now.
- **Direction filtering** — `_segment_moves_toward_destination` ensures only buses whose alighting stop is closer to the destination than the boarding stop are returned.
- **Capacity** — occupancy is sourced from AC Transit vehicle telemetry and stored via `POST /telemetry`. When no data is available the field is `null`; the frontend adds a small random noise offset for display realism.
