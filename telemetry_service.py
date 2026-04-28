from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional

import requests

from config import ACTRANSIT_API_KEY, ASSUMED_BUS_SEAT_CAPACITY, MAX_TELEMETRY_ENTRIES, VEHICLE_ATTRIBUTES_TTL_SECONDS
from http_client import _request_json
from schemas import Telemetry

latest_bus_metrics: Dict[str, dict] = {}
vehicle_attributes_cache: Dict[str, tuple[datetime, Optional[dict]]] = {}
_metrics_lock = Lock()
_vehicle_attributes_lock = Lock()

PSGLD_PERCENT = {
    "EMPTY": 10,
    "HALF_EMPTY": 50,
    "STANDING_ROOM_ONLY": 80,
    "FULL": 95,
}

PSGLD_LABEL = {
    "EMPTY": "Empty",
    "HALF_EMPTY": "Some seats available",
    "STANDING_ROOM_ONLY": "Standing room only",
    "FULL": "Full",
}


def _normalize_psgld(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    return normalized if normalized in PSGLD_PERCENT else None


def occupancy_from_psgld(psgld: Optional[str]) -> Optional[int]:
    return PSGLD_PERCENT.get(_normalize_psgld(psgld) or "")


def occupancy_label_from_psgld(psgld: Optional[str]) -> Optional[str]:
    return PSGLD_LABEL.get(_normalize_psgld(psgld) or "")


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


def _evict_expired_vehicle_attributes(now: datetime) -> None:
    expired = [
        key for key, (timestamp, _) in vehicle_attributes_cache.items()
        if (now - timestamp).total_seconds() >= VEHICLE_ATTRIBUTES_TTL_SECONDS
    ]
    for key in expired:
        vehicle_attributes_cache.pop(key, None)


def _fetch_vehicle_realtime_attributes(vehicle_id: str, route_name: str) -> Optional[dict]:
    if not ACTRANSIT_API_KEY:
        return None

    cache_key = f"{route_name}:{vehicle_id}"
    now = datetime.now(timezone.utc)
    with _vehicle_attributes_lock:
        cached = vehicle_attributes_cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < VEHICLE_ATTRIBUTES_TTL_SECONDS:
            return cached[1]
        _evict_expired_vehicle_attributes(now)
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
        with _vehicle_attributes_lock:
            vehicle_attributes_cache[cache_key] = (now, attributes)
        return attributes
    except requests.RequestException:
        with _vehicle_attributes_lock:
            vehicle_attributes_cache[cache_key] = (now, None)
        return None

def _capacity_for_prediction(prediction: dict, route_name: str) -> tuple[Optional[int], Optional[dict]]:
    vehicle_id = prediction.get("VehicleId")
    if vehicle_id is None:
        return None, None

    with _metrics_lock:
        metrics = latest_bus_metrics.get(str(vehicle_id)) or latest_bus_metrics.get(vehicle_id)
    if metrics:
        capacity = metrics.get("crowding_percentage")
        if capacity is not None:
            try:
                rounded_capacity = round(float(capacity))
            except (TypeError, ValueError):
                rounded_capacity = None
            if rounded_capacity is not None:
                return rounded_capacity, {
                    "source": "telemetry",
                    "vehicle_id": str(vehicle_id),
                    "estimated_occupancy_status_color": None,
                    "estimated_occupancy_status": None,
                    "estimated_occupancy_percentage": rounded_capacity,
                }

    attributes = _fetch_vehicle_realtime_attributes(str(vehicle_id), route_name)
    if not attributes:
        return None, None

    occupancy_color = attributes.get("EstimatedOccupancyStatusColor")
    mapped_capacity = _occupancy_from_status_color(occupancy_color)
    if mapped_capacity is None and attributes.get("EstimatedOccupancyPercentage") is not None:
        try:
            mapped_capacity = round(float(attributes["EstimatedOccupancyPercentage"]))
        except (TypeError, ValueError):
            mapped_capacity = None

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


def store_telemetry(data: Telemetry) -> dict:
    record = data.model_dump()
    if record["timestamp"] is None:
        record["timestamp"] = datetime.now(timezone.utc).isoformat()

    with _metrics_lock:
        latest_bus_metrics[data.bus_id] = record
        if len(latest_bus_metrics) > MAX_TELEMETRY_ENTRIES:
            oldest_bus = min(
                latest_bus_metrics,
                key=lambda bus_id: latest_bus_metrics[bus_id].get("timestamp") or "",
            )
            latest_bus_metrics.pop(oldest_bus, None)
    return {"status": "success", "stored_bus_id": data.bus_id}


def get_bus_metric(bus_id: str) -> Optional[dict]:
    with _metrics_lock:
        metric = latest_bus_metrics.get(bus_id)
        return dict(metric) if metric is not None else None


def _vehicle_occupancy_for_marker(
    vehicle_id: Optional[str],
    route_name: str,
    psgld: Optional[str] = None,
) -> Optional[int]:
    if not vehicle_id:
        return occupancy_from_psgld(psgld)
    metric = get_bus_metric(str(vehicle_id))
    if metric and metric.get("crowding_percentage") is not None:
        try:
            return round(float(metric["crowding_percentage"]))
        except (TypeError, ValueError):
            pass

    psgld_percent = occupancy_from_psgld(psgld)
    if psgld_percent is not None:
        return psgld_percent

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


def occupancy_seats_left(capacity: Optional[int]) -> Optional[int]:
    if capacity is None:
        return None
    return max(ASSUMED_BUS_SEAT_CAPACITY - round(ASSUMED_BUS_SEAT_CAPACITY * capacity / 100), 1)
