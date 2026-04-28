import csv
import os
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional

from fastapi import HTTPException

from config import GTFS_STATIC_DIR, MAX_GTFS_STOP_DISTANCE_METERS, WALK_SPEED_METERS_PER_MINUTE
from common import (
    _ac_transit_now,
    _distance_meters,
    _get_any,
    _get_stripped,
    _normalize_text,
    _route_label,
    _text_tokens,
    _time_to_seconds,
    _try_int,
)

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
        if (trip_id := _get_stripped(row, "trip_id"))
    }


@lru_cache(maxsize=1)
def _load_gtfs_routes_by_id() -> Dict[str, dict]:
    return {
        route_id: row
        for row in _load_gtfs_routes()
        if (route_id := _get_stripped(row, "route_id"))
    }


@lru_cache(maxsize=1)
def _load_gtfs_stops_by_id() -> Dict[str, dict]:
    _ensure_gtfs_files_exist()
    stops: Dict[str, dict] = {}
    with open(_gtfs_file_path("stops.txt"), newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            stop_id = _get_stripped(row, "stop_id")
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
    return min(
        stops_by_id,
        key=lambda stop_id: _distance_meters(point, stops_by_id[stop_id]),
    )


def _nearest_gtfs_stop(point: dict, preferred_name: Optional[str] = None) -> Optional[dict]:
    stops_by_id = _load_gtfs_stops_by_id()
    if not stops_by_id:
        return None

    normalized_preferred_name = _normalize_text(preferred_name)

    def score(stop: dict) -> float:
        distance = _distance_meters(point, stop)
        if not normalized_preferred_name:
            return distance
        normalized_stop_name = _normalize_text(stop["stop_name"])
        if normalized_stop_name == normalized_preferred_name:
            return distance - 200
        if normalized_preferred_name in normalized_stop_name or normalized_stop_name in normalized_preferred_name:
            return distance - 120
        return distance + 120

    return min(stops_by_id.values(), key=score)


def _nearest_gtfs_stops(
    point: dict,
    limit: int = 4,
    max_distance_meters: int = MAX_GTFS_STOP_DISTANCE_METERS,
) -> List[dict]:
    stops_by_id = _load_gtfs_stops_by_id()
    nearby = [
        (_distance_meters(point, stop), stop)
        for stop in stops_by_id.values()
    ]
    nearby = [pair for pair in nearby if pair[0] <= max_distance_meters]
    nearby.sort(key=lambda pair: pair[0])
    return [stop for _, stop in nearby[:limit]]


def _parse_gtfs_time_to_seconds(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return _time_to_seconds(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _load_gtfs_stop_times_by_trip() -> Dict[str, List[dict]]:
    _ensure_gtfs_files_exist()
    by_trip: Dict[str, List[dict]] = {}
    with open(_gtfs_file_path("stop_times.txt"), newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            trip_id = _get_stripped(row, "trip_id")
            stop_id = _get_stripped(row, "stop_id")
            stop_sequence = _try_int(row.get("stop_sequence"))
            if not trip_id or not stop_id or stop_sequence is None:
                continue
            arrival_time = _get_stripped(row, "arrival_time")
            departure_time = _get_stripped(row, "departure_time")
            shape_distance_raw = row.get("shape_dist_traveled")
            try:
                shape_dist_traveled = float(shape_distance_raw) if shape_distance_raw not in (None, "") else None
            except ValueError:
                shape_dist_traveled = None
            by_trip.setdefault(trip_id, []).append(
                {
                    "stop_id": stop_id,
                    "stop_sequence": stop_sequence,
                    "arrival_time": arrival_time,
                    "departure_time": departure_time,
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
        for row in csv.DictReader(handle):
            shape_id = _get_stripped(row, "shape_id")
            if not shape_id:
                continue
            lat_raw = row.get("shape_pt_lat")
            lng_raw = row.get("shape_pt_lon")
            if lat_raw is None or lng_raw is None:
                continue
            try:
                lat = float(lat_raw)
                lng = float(lng_raw)
            except ValueError:
                continue
            sequence = _try_int(row.get("shape_pt_sequence")) or 0
            shapes_by_id.setdefault(shape_id, []).append(
                {"lat": lat, "lng": lng, "sequence": sequence}
            )
    for shape_points in shapes_by_id.values():
        shape_points.sort(key=lambda point: point["sequence"])
    return shapes_by_id


@lru_cache(maxsize=1)
def _gtfs_trip_directions() -> Dict[str, Optional[int]]:
    return {
        trip_id: _try_int(row.get("direction_id"))
        for row in _load_gtfs_trips()
        if (trip_id := _get_stripped(row, "trip_id"))
    }


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
            date_key = _get_stripped(row, "date")
            if not date_key:
                continue
            by_date.setdefault(date_key, []).append(row)
    return by_date


def _resolve_route_ids(route_identifier: str) -> List[str]:
    route_key = (route_identifier or "").strip()
    if not route_key:
        return []
    routes = _load_gtfs_routes()
    by_id = [route_id for row in routes if (route_id := _get_stripped(row, "route_id")) == route_key]
    if by_id:
        return by_id
    return [
        route_id
        for row in routes
        if _get_stripped(row, "route_short_name") == route_key
        and (route_id := _get_stripped(row, "route_id"))
    ]


def _nearest_shape_index(shape_points: List[dict], point: dict) -> int:
    if not shape_points:
        return -1
    return min(
        range(len(shape_points)),
        key=lambda index: _distance_meters(shape_points[index], point),
    )


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
    candidates: List[tuple[float, dict]] = []
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
        walk_distance = _distance_meters(destination_point, stop)
        walk_minutes = max(round(walk_distance / WALK_SPEED_METERS_PER_MINUTE), 1)
        ride_minutes = _ride_minutes_between(boarding_stop, candidate)
        score = ride_minutes + walk_minutes
        if destination_stop_id and stop["stop_id"] == destination_stop_id:
            score -= 12
        if preferred_alighting_stop_id and stop["stop_id"] == preferred_alighting_stop_id:
            score -= 16
        candidates.append((score, candidate))
    if not candidates:
        return None
    return min(candidates, key=lambda pair: pair[0])[1]


def _public_stop_view(stop: dict) -> dict:
    return {
        "stop_id": stop["stop_id"],
        "stop_name": stop["stop_name"],
        "lat": stop["lat"],
        "lng": stop["lng"],
        "stop_sequence": stop["stop_sequence"],
    }


def _build_segment_geometry(
    shape_points: List[dict],
    boarding_index: int,
    alighting_index: int,
    preferred_segment_geometry: Optional[dict],
) -> Optional[tuple[dict, dict]]:
    """Returns (segment_geometry, full_geometry) or None if the slice is too short."""
    shape_segment_points = shape_points[boarding_index: alighting_index + 1]
    if len(shape_segment_points) < 2:
        return None
    full_geometry = {
        "type": "LineString",
        "coordinates": [[p["lng"], p["lat"]] for p in shape_points],
    }
    segment_geometry = preferred_segment_geometry or {
        "type": "LineString",
        "coordinates": [[p["lng"], p["lat"]] for p in shape_segment_points],
    }
    return segment_geometry, full_geometry


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
    trip_id = _get_stripped(trip, "trip_id")
    shape_id = _get_stripped(trip, "shape_id")
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

    if boarding_stop["stop_sequence"] >= alighting_stop["stop_sequence"]:
        return None

    boarding_index = _nearest_shape_index(shape_points, boarding_stop)
    alighting_index = _nearest_shape_index(shape_points, alighting_stop)
    if boarding_index < 0 or alighting_index < 0 or boarding_index > alighting_index:
        return None

    geometry = _build_segment_geometry(
        shape_points, boarding_index, alighting_index, preferred_segment_geometry
    )
    if geometry is None:
        return None
    segment_geometry, full_geometry = geometry

    segment_stops = _segment_stops_for_trip(
        trip_stop_times,
        stops_by_id,
        boarding_stop["stop_sequence"],
        alighting_stop["stop_sequence"],
        boarding_stop["stop_id"],
        alighting_stop["stop_id"],
    )

    route_id = _get_stripped(trip, "route_id") or ""
    route = _load_gtfs_routes_by_id().get(route_id, {})

    return {
        "trip_id": trip_id,
        "route_id": route_id,
        "route_short_name": _route_label(route, route_id),
        "route_long_name": _get_any(route, "route_long_name", "route_short_name") or route_id,
        "direction_id": _try_int(trip.get("direction_id")),
        "shape_id": shape_id,
        "trip_headsign": _get_stripped(trip, "trip_headsign"),
        "boarding_stop": _public_stop_view(boarding_stop),
        "alighting_stop": _public_stop_view(alighting_stop),
        "boarding_before_alighting": True,
        "ride_time_minutes": _ride_minutes_between(boarding_stop, alighting_stop),
        "segment_stops": segment_stops,
        "geometry": segment_geometry,
        "geometry_source": "google_transit" if preferred_segment_geometry else "gtfs_static",
        "full_route_geometry": full_geometry,
    }


def _resolve_segment_route_ids(
    route_name: str,
    google_route_id: Optional[str],
    google_route_short_name: Optional[str],
) -> List[str]:
    route_ids = _resolve_route_ids(route_name)
    if google_route_id and google_route_id not in route_ids:
        route_ids = [google_route_id, *route_ids]
    if not route_ids and google_route_short_name:
        route_ids = _resolve_route_ids(google_route_short_name)
    return route_ids


def _score_trip_for_segment(
    trip: dict,
    *,
    direction_hint: Optional[int],
    preferred_headsign: str,
    google_route_id: Optional[str],
) -> int:
    score = 0
    trip_direction = _try_int(trip.get("direction_id"))
    trip_headsign = _normalize_text(trip.get("trip_headsign"))
    if direction_hint is not None and trip_direction is not None:
        score += 30 if trip_direction == direction_hint else -10
    if preferred_headsign:
        if trip_headsign == preferred_headsign:
            score += 35
        elif preferred_headsign in trip_headsign or trip_headsign in preferred_headsign:
            score += 18
    if google_route_id and _get_stripped(trip, "route_id") == google_route_id:
        score += 12
    return score


def _ordered_segment_candidates(
    trips: List[dict],
    selected_trip: Optional[dict],
    route_ids: List[str],
    *,
    direction_hint: Optional[int],
    preferred_headsign: str,
    google_route_id: Optional[str],
) -> List[dict]:
    candidate_trips = [trip for trip in trips if _get_stripped(trip, "route_id") in route_ids]
    prioritized: List[tuple[int, dict]] = []
    if selected_trip:
        selected_trip_route_id = _get_stripped(selected_trip, "route_id")
        if not route_ids or selected_trip_route_id in route_ids:
            prioritized.append((100, selected_trip))
    for trip in candidate_trips:
        prioritized.append((
            _score_trip_for_segment(
                trip,
                direction_hint=direction_hint,
                preferred_headsign=preferred_headsign,
                google_route_id=google_route_id,
            ),
            trip,
        ))

    seen: set = set()
    ordered: List[dict] = []
    for _, trip in sorted(prioritized, key=lambda item: item[0], reverse=True):
        candidate_id = _get_stripped(trip, "trip_id")
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        ordered.append(trip)
    return ordered


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

    selected_trip = next(
        (trip for trip in trips if _get_stripped(trip, "trip_id") == str(trip_id)),
        None,
    ) if trip_id is not None else None

    candidate = google_candidate or {}
    route_ids = _resolve_segment_route_ids(
        route_name,
        candidate.get("route_id"),
        candidate.get("route_short_name"),
    )
    direction_hint = _try_int(selected_trip.get("direction_id")) if selected_trip else None
    if direction_hint is None:
        direction_hint = prediction_direction_id
    preferred_headsign = _normalize_text(candidate.get("trip_headsign"))

    ordered_candidates = _ordered_segment_candidates(
        trips,
        selected_trip,
        route_ids,
        direction_hint=direction_hint,
        preferred_headsign=preferred_headsign,
        google_route_id=candidate.get("route_id"),
    )

    preferred_alighting_stop_id = candidate.get("alighting_stop_id") or destination_stop_id
    preferred_segment_geometry = candidate.get("segment_geometry")
    expected_boarding_stop_id = candidate.get("boarding_stop_id")
    expected_alighting_stop_id = candidate.get("alighting_stop_id")

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
        if (
            expected_alighting_stop_id
            and segment["alighting_stop"]["stop_id"] != expected_alighting_stop_id
            and preferred_segment_geometry
        ):
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
    route_key = (route_id or "").strip()
    if not route_key:
        raise HTTPException(status_code=400, detail="route_id is required")

    trips = _load_gtfs_trips()
    matching_trips = [row for row in trips if _get_stripped(row, "route_id") == route_key]

    if not matching_trips:
        route_ids = _resolve_route_ids(route_key)
        if route_ids:
            matching_trips = [row for row in trips if _get_stripped(row, "route_id") in route_ids]

    if direction_id is not None:
        matching_trips = [
            row for row in matching_trips
            if _try_int(row.get("direction_id")) == direction_id
        ]

    shape_ids = [shape_id for row in matching_trips if (shape_id := _get_stripped(row, "shape_id"))]
    if not shape_ids:
        return []

    shape_id = Counter(shape_ids).most_common(1)[0][0]
    shape_points = _load_gtfs_shapes().get(shape_id, [])
    return [{"lat": point["lat"], "lng": point["lng"]} for point in shape_points]

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
