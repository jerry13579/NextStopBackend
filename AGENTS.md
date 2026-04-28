# Agent rules — NextStop Backend

## Rules

- Do not use placeholder or dummy values anywhere unless explicitly instructed
- Do not add or remove features without permission
- Do not read `.env` files directly
- Do not run git commands
- Remove old or deprecated code rather than leaving it commented out

## Architecture

Single file: `main.py`. All endpoints, business logic, data loading, and helpers live here.

**Runtime dependencies:**
- FastAPI + Uvicorn (HTTP server)
- Pydantic (request/response models)
- Requests (outbound HTTP to AC Transit and Google APIs)
- SQLite via Python's `sqlite3` (user auth, stored calendar events)
- Python standard library `csv`, `zipfile`, `lru_cache` (GTFS loading)

**Data flow for `/plan-trip`:**
1. `_fetch_nearby_stops` → AC Transit REST → list of nearby stops
2. `_google_transit_candidates` → Google Routes TRANSIT mode → candidate routes with boarding/alighting stops and departure times
3. `_fetch_predictions` → AC Transit predictions per stop → falls back to `_scheduled_predictions_for_stop` (GTFS)
4. `_select_route_planning_predictions` → scores predictions against Google candidates
5. `_build_available_buses` → for each prediction, calls `_build_transit_segment` (GTFS) and `_google_route` (walking legs)

**GTFS loading:**  
All GTFS files are loaded with `@lru_cache(maxsize=1)` functions at startup. Modifying these functions invalidates the cache on the next call. Never clear the cache manually — restart the process instead.

**Calendar commute previews:**  
`_schedule_commute_preview_for_event` is called once per upcoming class event. It calls `_google_transit_candidates` with `departure_time=class_start` (not `now()`) to get routes that run at the class's actual time. It then matches those candidates against GTFS stop times filtered to the active service IDs for that date.

## Key functions to know before editing

| Function | What it does |
|---|---|
| `_build_transit_segment` | Finds the best GTFS trip+segment for a given route and boarding stop toward a destination |
| `_evaluate_trip_segment` | Picks boarding/alighting stops on a specific trip and returns geometry |
| `_pick_alighting_stop` | Scores candidate alighting stops by `ride_time + walk_to_destination`; bonuses for Google-suggested stop |
| `_segment_moves_toward_destination` | Returns False if alighting stop is farther from destination than boarding stop — used to drop wrong-direction buses |
| `_active_gtfs_service_ids` | Returns the set of GTFS service_ids active on a given date, applying calendar_dates exceptions |
| `_google_transit_candidates` | Calls Google Routes in TRANSIT mode; accepts optional `departure_time` for future class times |
| `_schedule_commute_preview_for_event` | Builds a `CalendarCommutePreview` for one class event using GTFS + Google candidates |

## API keys

| Variable | Required for |
|---|---|
| `ACTRANSIT_API_KEY` | Live stop predictions, nearby stop lookup |
| `GOOGLE_MAPS_API` | Geocoding, walking routes, transit candidates, Maps JS key delivery |
| `SECRET_KEY` | Signing session tokens |

Without `ACTRANSIT_API_KEY` the app falls back to GTFS schedule data. Without `GOOGLE_MAPS_API` geocoding, walking legs, and transit candidates are unavailable and most responses will be `source: "unavailable"`.

## Deployment

Deployed on Azure App Service. Startup command: `uvicorn main:app --host 0.0.0.0 --port 8000`. The GTFS zip is downloaded from AC Transit on first startup and extracted to the `gtfs/` directory. If the directory is missing or the files are stale, delete it and restart.
