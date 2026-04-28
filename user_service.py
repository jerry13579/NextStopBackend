from datetime import datetime, timezone
from typing import Any

from database import _db, _preference_defaults
from schemas import UserPreferences, UserPreferencesPatch


def get_preferences(user_id: int) -> UserPreferences:
    with _db() as connection:
        row = connection.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            defaults = _preference_defaults()
            connection.execute(
                """
                INSERT INTO user_preferences
                (user_id, smart_departure_alerts, capacity_alerts, delay_notifications, theme_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, int(defaults["smart_departure_alerts"]), int(defaults["capacity_alerts"]), int(defaults["delay_notifications"]), defaults["theme_id"], datetime.now(timezone.utc).isoformat()),
            )
            return UserPreferences(**defaults)
    return UserPreferences(
        smart_departure_alerts=bool(row["smart_departure_alerts"]),
        capacity_alerts=bool(row["capacity_alerts"]),
        delay_notifications=bool(row["delay_notifications"]),
        theme_id=row["theme_id"],
    )


def update_preferences(user_id: int, patch: UserPreferencesPatch) -> UserPreferences:
    current = get_preferences(user_id).model_dump()
    current.update(patch.model_dump(exclude_unset=True))
    with _db() as connection:
        connection.execute(
            """
            UPDATE user_preferences
            SET smart_departure_alerts = ?, capacity_alerts = ?, delay_notifications = ?, theme_id = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (int(current["smart_departure_alerts"]), int(current["capacity_alerts"]), int(current["delay_notifications"]), current["theme_id"], datetime.now(timezone.utc).isoformat(), user_id),
        )
    return UserPreferences(**current)


def _rows(connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def export_user_data(user_id: int) -> dict:
    preferences = get_preferences(user_id).model_dump()
    with _db() as connection:
        return {
            "preferences": preferences,
            "calendar_events": _rows(connection, "SELECT external_id, title, location, start_time, end_time, source FROM calendar_events WHERE user_id = ?", (user_id,)),
            "trips": _rows(connection, "SELECT class_id, class_code, class_name, scheduled_time, actual_arrival_time, was_on_time, mode, bus_line, distance_km, steps, calories_burned, transit_minutes FROM trips WHERE user_id = ?", (user_id,)),
            "favorites": _rows(connection, "SELECT eatery_id, created_at FROM local_eat_favorites WHERE user_id = ?", (user_id,)),
            "saved_places": [],
            "notification_settings": preferences,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }


def clear_user_data(user_id: int) -> dict:
    with _db() as connection:
        connection.execute("DELETE FROM calendar_events WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM trips WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM local_eat_favorites WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
    return {"status": "ok"}
