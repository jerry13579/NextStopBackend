import json
from datetime import datetime, timezone
from typing import List, Optional

import requests

from common import _distance_meters
from config import GOOGLE_MAPS_API, UC_BERKELEY_CENTER
from database import _db, _json_or_none
from google_service import _places_location_bias
from http_client import _google_routes_headers, _post_json
from schemas import LocalEat, StudentDiscount

CATEGORY_QUERIES = {
    "cafe": "cafes near UC Berkeley",
    "restaurant": "restaurants near UC Berkeley",
    "quick-bites": "quick bites near UC Berkeley",
    "dessert": "dessert near UC Berkeley",
}
CATEGORY_EMOJI = {
    "cafe": "\u2615",
    "restaurant": "\U0001f37d\ufe0f",
    "quick-bites": "\U0001f96a",
    "dessert": "\U0001f366",
}


def _category_from_types(types: List[str], requested_category: Optional[str]) -> str:
    if requested_category in CATEGORY_QUERIES:
        return requested_category
    normalized = set(types or [])
    if "cafe" in normalized or "coffee_shop" in normalized:
        return "cafe"
    if "bakery" in normalized or "ice_cream_shop" in normalized or "dessert_shop" in normalized:
        return "dessert"
    if "meal_takeaway" in normalized or "fast_food_restaurant" in normalized:
        return "quick-bites"
    return "restaurant"


def _price_level(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.replace("PRICE_LEVEL_", "").replace("UNSPECIFIED", "").lower() or None


def _discount_from_json(raw: Optional[str]) -> Optional[StudentDiscount]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return StudentDiscount(**data) if data else None


def _favorite_ids(user_id: Optional[int]) -> set[str]:
    if user_id is None:
        return set()
    with _db() as connection:
        rows = connection.execute("SELECT eatery_id FROM local_eat_favorites WHERE user_id = ?", (user_id,)).fetchall()
    return {row["eatery_id"] for row in rows}


def _stored_discount_by_id() -> dict[str, Optional[str]]:
    with _db() as connection:
        rows = connection.execute("SELECT id, student_discount_json FROM local_eats").fetchall()
    return {row["id"]: row["student_discount_json"] for row in rows}


def _place_to_eatery(place: dict, category: Optional[str], user_location: Optional[dict], favorites: set[str], discounts: dict[str, Optional[str]]) -> LocalEat:
    place_id = place["id"]
    types = place.get("types") or []
    item_category = _category_from_types(types, category)
    location = place.get("location") or {}
    distance_miles = None
    walk_time_minutes = None
    if user_location and location.get("latitude") is not None and location.get("longitude") is not None:
        meters = _distance_meters(user_location, {"lat": location["latitude"], "lng": location["longitude"]})
        distance_miles = round(meters / 1609.344, 2)
        walk_time_minutes = max(round(meters / 80), 1)
    opening_hours = place.get("regularOpeningHours") or place.get("currentOpeningHours") or {}
    hours = opening_hours.get("weekdayDescriptions")
    discount = _discount_from_json(discounts.get(place_id))
    return LocalEat(
        id=place_id,
        name=(place.get("displayName") or {}).get("text") or place.get("formattedAddress") or place_id,
        type=item_category,
        emoji=CATEGORY_EMOJI.get(item_category, "restaurant"),
        rating=place.get("rating"),
        price_level=_price_level(place.get("priceLevel")),
        distance_miles=distance_miles,
        walk_time_minutes=walk_time_minutes,
        address=place.get("formattedAddress"),
        hours=hours,
        description=", ".join(types[:3]) if types else None,
        is_open=(place.get("currentOpeningHours") or opening_hours).get("openNow"),
        is_favorite=place_id in favorites,
        cuisine_type=types[0].replace("_", " ") if types else None,
        student_discount=discount,
    )


def _persist_eateries(eateries: List[LocalEat], locations_by_id: dict[str, dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.executemany(
            """
            INSERT INTO local_eats
            (id, name, category, type, emoji, rating, price_level, lat, lng, address, hours_json,
             description, is_open, cuisine_type, student_discount_json, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                category=excluded.category,
                type=excluded.type,
                emoji=excluded.emoji,
                rating=excluded.rating,
                price_level=excluded.price_level,
                lat=excluded.lat,
                lng=excluded.lng,
                address=excluded.address,
                hours_json=excluded.hours_json,
                description=excluded.description,
                is_open=excluded.is_open,
                cuisine_type=excluded.cuisine_type,
                last_verified_at=excluded.last_verified_at
            """,
            [
                (
                    eatery.id,
                    eatery.name,
                    eatery.type,
                    eatery.type,
                    eatery.emoji,
                    eatery.rating,
                    eatery.price_level,
                    (locations_by_id.get(eatery.id) or {}).get("lat"),
                    (locations_by_id.get(eatery.id) or {}).get("lng"),
                    eatery.address,
                    _json_or_none(eatery.hours),
                    eatery.description,
                    None if eatery.is_open is None else int(eatery.is_open),
                    eatery.cuisine_type,
                    _json_or_none(eatery.student_discount.model_dump() if eatery.student_discount else None),
                    now,
                )
                for eatery in eateries
            ],
        )


def _stored_eateries(category: Optional[str], user_location: Optional[dict], favorites: set[str], favorites_only: bool, deals_only: bool) -> List[LocalEat]:
    clauses = []
    params: list = []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if deals_only:
        clauses.append("student_discount_json IS NOT NULL")
    sql = "SELECT * FROM local_eats"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with _db() as connection:
        rows = connection.execute(sql, params).fetchall()
    eateries: List[LocalEat] = []
    for row in rows:
        if favorites_only and row["id"] not in favorites:
            continue
        distance_miles = None
        walk_time_minutes = None
        if user_location and row["lat"] is not None and row["lng"] is not None:
            meters = _distance_meters(user_location, {"lat": row["lat"], "lng": row["lng"]})
            distance_miles = round(meters / 1609.344, 2)
            walk_time_minutes = max(round(meters / 80), 1)
        eateries.append(
            LocalEat(
                id=row["id"],
                name=row["name"],
                type=row["type"],
                emoji=row["emoji"],
                rating=row["rating"],
                price_level=row["price_level"],
                distance_miles=distance_miles,
                walk_time_minutes=walk_time_minutes,
                address=row["address"],
                hours=json.loads(row["hours_json"]) if row["hours_json"] else None,
                description=row["description"],
                is_open=None if row["is_open"] is None else bool(row["is_open"]),
                is_favorite=row["id"] in favorites,
                cuisine_type=row["cuisine_type"],
                student_discount=_discount_from_json(row["student_discount_json"]),
            )
        )
    return sorted(eateries, key=lambda item: (item.distance_miles is None, item.distance_miles or 9999, item.name))


def get_local_eats(category: Optional[str], deals_only: bool, favorites_only: bool, lat: Optional[float], lng: Optional[float], user_id: Optional[int]) -> tuple[List[LocalEat], str]:
    user_location = {"lat": lat, "lng": lng} if lat is not None and lng is not None else UC_BERKELEY_CENTER
    favorites = _favorite_ids(user_id)
    if favorites_only and user_id is None:
        return [], "sqlite"
    if category and category not in CATEGORY_QUERIES:
        category = None
    if GOOGLE_MAPS_API and not favorites_only:
        payload = {"textQuery": CATEGORY_QUERIES.get(category or "restaurant", CATEGORY_QUERIES["restaurant"]), "maxResultCount": 12, "locationBias": _places_location_bias(user_location)}
        headers = _google_routes_headers(
            "places.id,places.displayName,places.formattedAddress,places.location,places.rating,"
            "places.priceLevel,places.types,places.regularOpeningHours,places.currentOpeningHours,places.businessStatus"
        )
        try:
            data = _post_json("https://places.googleapis.com/v1/places:searchText", payload, headers)
            discounts = _stored_discount_by_id()
            places = [place for place in data.get("places", []) if place.get("id")]
            locations_by_id = {
                place["id"]: {
                    "lat": place["location"]["latitude"],
                    "lng": place["location"]["longitude"],
                }
                for place in places
                if (place.get("location") or {}).get("latitude") is not None
                and (place.get("location") or {}).get("longitude") is not None
            }
            eateries = [_place_to_eatery(place, category, user_location, favorites, discounts) for place in places]
            if deals_only:
                eateries = [eatery for eatery in eateries if eatery.student_discount is not None]
            _persist_eateries(eateries, locations_by_id)
            return sorted(eateries, key=lambda item: (item.distance_miles is None, item.distance_miles or 9999, item.name)), "google_places"
        except requests.RequestException as exc:
            print(f"[local_eats] Google Places lookup failed: {exc!r}")
    return _stored_eateries(category, user_location, favorites, favorites_only, deals_only), "sqlite"


def set_favorite(user_id: int, eatery_id: str) -> dict:
    with _db() as connection:
        exists = connection.execute("SELECT 1 FROM local_eats WHERE id = ?", (eatery_id,)).fetchone()
        if exists is None:
            return {"status": "not_found", "is_favorite": False}
        connection.execute("INSERT OR IGNORE INTO local_eat_favorites (user_id, eatery_id, created_at) VALUES (?, ?, ?)", (user_id, eatery_id, datetime.now(timezone.utc).isoformat()))
    return {"status": "ok", "is_favorite": True}


def clear_favorite(user_id: int, eatery_id: str) -> dict:
    with _db() as connection:
        connection.execute("DELETE FROM local_eat_favorites WHERE user_id = ? AND eatery_id = ?", (user_id, eatery_id))
    return {"status": "ok", "is_favorite": False}
