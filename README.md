# NextStop Backend

FastAPI backend for trip planning using real AC Transit, GTFS, and Google Maps data.

## Features

* Browser-ready CORS for local frontend development
* `GET /plan-trip` accepts the user's latitude/longitude, finds nearby active AC Transit stops, returns live predictions, and includes Google Routes walking times when configured
* `GET /next-bus/{stop_id}` returns the next predicted departure
* `POST /telemetry` stores crowding data for future real-time enhancements
* `POST /calendar/import` imports `.ics` calendar files into class events

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Optional `.env`:

```bash
ACTRANSIT_API_KEY=your_key_here
GOOGLE_MAPS_API=your_google_maps_platform_key
# Optional comma-separated overrides
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

## Frontend Integration

The extracted frontend in `../frontend` expects the backend at `http://127.0.0.1:8000` by default. Override it in `frontend/.env` if needed:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000
```

## Notes

For production routing, set both `ACTRANSIT_API_KEY` and `GOOGLE_MAPS_API`.

* AC Transit supplies nearby stop matching and live stop predictions.
* Google Places supplies destination search/geocoding.
* Google Routes supplies walking duration/distance from the user to the stop and from the stop to the destination.
* Google Maps JavaScript renders the web map through `/config/maps`.
* Capacity is only live when posted through `/telemetry`; otherwise the API returns `null` instead of a guessed value.

Without the required API keys, endpoints return unavailable/error states instead of demo predictions or guessed route data.

## Render Deployment

This repo includes a Render Blueprint at `../render.yaml`.

### Deploy From Blueprint

1. Push the repo to GitHub.
2. In Render, create a new Blueprint and select this repo.
3. Render will use:
   * Root directory: `backend`
   * Build command: `pip install -r requirements.txt`
   * Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   * Health check path: `/health`
4. Add environment variables in Render:

```bash
ACTRANSIT_API_KEY=your_actransit_key
GOOGLE_MAPS_API=your_google_maps_platform_key
CORS_ORIGINS=https://your-frontend-domain.com,http://localhost:5173
```

Do not deploy production without `ACTRANSIT_API_KEY` and `GOOGLE_MAPS_API`; route planning depends on real service and map data.

### Manual Render Settings

If you create a Web Service manually instead of using the Blueprint:

```bash
Root Directory: backend
Runtime: Python
Build Command: pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
Health Check Path: /health
```

After deployment, verify:

```bash
curl https://your-render-service.onrender.com/health
curl "https://your-render-service.onrender.com/plan-trip?destination=Soda%20Hall%20310&start_lat=37.8719&start_lng=-122.2585"
```
