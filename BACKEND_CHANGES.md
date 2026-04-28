# Backend Changes Implemented

## Module Split

`main.py` is now the FastAPI entrypoint and endpoint wiring. The old monolith logic was split by responsibility:

- `config.py`: environment-backed settings, CORS origins, tunables, shared constants.
- `database.py`: SQLite connection lifecycle and idempotent schema creation.
- `schemas.py`: Pydantic request and response models.
- `common.py`: parsing, time, distance, polyline, and coordinate validation helpers.
- `http_client.py`: shared `requests` wrappers and Google Routes request builders.
- `gtfs_service.py`: GTFS loading, stop matching, service calendar logic, and transit segment construction.
- `google_service.py`: Google Routes, transit candidates, geocoding, and Places location bias helpers.
- `transit_service.py`: AC Transit calls, prediction selection, bus option construction, and `/plan-trip` response assembly.
- `telemetry_service.py`: telemetry storage, vehicle occupancy cache, and realtime vehicle attributes.
- `calendar_service.py`: ICS parsing, recurring event expansion, calendar storage, and commute previews.
- `local_eats_service.py`: Local Eats lookup, caching, discounts, and favorites.
- `trip_service.py`: trip logging, recent trip history, summaries, and streaks.
- `user_service.py`: preferences, export, and user-owned data clearing.

## Product API Work

- Added `GET /local-eats` with `category`, `deals_only`, `favorites_only`, `lat`, and `lng` filters.
- Added `PUT /local-eats/{eatery_id}/favorite` and `DELETE /local-eats/{eatery_id}/favorite`.
- Local Eats uses Google Places when configured, caches real returned places in SQLite, and persists user favorites.
- Added `POST /trips` with idempotent trip completion logging.
- Added `GET /trips?from=&to=` with the required `from` query alias.
- Added `GET /stats/summary?period=today|week|semester` with trip count, on-time rate, streak, distance, steps, calories, CO2, and transit minutes.
- Extended `GET /plan-trip` with `mode=bus` and `include_walk=false`; bus-only requests skip direct-walk filtering and omit `direct_walk`.
- Preserved stable route metadata in bus options: option id, route id/short name, direction id, and boarding stop id.
- Added `GET /user/preferences` and `PATCH /user/preferences`.
- Added `GET /user/export` for preferences, calendar data, trips, favorites, saved places, and notification settings.
- Added `DELETE /user/data` to clear user-owned app data while preserving account and session records.
- Password reset endpoints were not added because no email delivery configuration exists in the backend.

## Shaky Code Fixed

- Replaced the mutable default list on `CalendarCommutePreview.options` with `Field(default_factory=list)`.
- Added coordinate validation for location search, reverse geocoding, and trip planning.
- Guarded empty calendar delete requests so SQLite never receives an empty `IN ()` clause.
- Added thread locks around in-memory telemetry and vehicle occupancy caches.
- Kept telemetry storage bounded with `MAX_TELEMETRY_ENTRIES`.
- Guarded occupancy percentage parsing so malformed API values do not crash route marker responses.
- Preserved local-eat place coordinates in SQLite so cached fallback results can still sort by distance.
- Replaced silent fallback branches with logged fallback messages where external lookups fail.
- Centralized API timeouts and Google request builders in `http_client.py`.
- Added nonnegative Pydantic validation for trip distance, steps, calories, and transit minutes.

## Verification

- `./.venv/bin/python -m py_compile *.py`
- `./.venv/bin/python -c "import main; print(main.app.title); print(len(main.app.routes))"`
- In-process smoke check against a temporary SQLite database for auth, telemetry, preferences, trips, stats, Local Eats fallback, export, and data clearing.
- OpenAPI check confirmed the new product endpoints and the `GET /trips` `from` alias.
