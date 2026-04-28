from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import CO2_GRAMS_PER_KM_CAR
from database import _db
from schemas import StatsSummary, TripLogRequest, TripResponse


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trip_row_to_response(row) -> TripResponse:
    arrival = _parse_iso(row["actual_arrival_time"])
    return TripResponse(
        id=row["id"],
        date=arrival.date().isoformat(),
        class_code=row["class_code"],
        class_name=row["class_name"],
        scheduled_time=row["scheduled_time"],
        actual_arrival_time=row["actual_arrival_time"],
        was_on_time=bool(row["was_on_time"]),
        mode=row["mode"],
        bus_line=row["bus_line"],
        distance_km=row["distance_km"],
        steps=row["steps"],
        calories_burned=row["calories_burned"],
        transit_minutes=row["transit_minutes"],
    )


def log_trip(user_id: int, request: TripLogRequest) -> TripResponse:
    scheduled = _parse_iso(request.scheduled_time)
    actual = _parse_iso(request.actual_arrival_time)
    was_on_time = actual <= scheduled
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.execute(
            """
            INSERT INTO trips
            (user_id, idempotency_key, class_id, class_code, class_name, scheduled_time, actual_arrival_time,
             was_on_time, mode, bus_line, distance_km, steps, calories_burned, transit_minutes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, idempotency_key) DO UPDATE SET
                class_id=excluded.class_id,
                class_code=excluded.class_code,
                class_name=excluded.class_name,
                scheduled_time=excluded.scheduled_time,
                actual_arrival_time=excluded.actual_arrival_time,
                was_on_time=excluded.was_on_time,
                mode=excluded.mode,
                bus_line=excluded.bus_line,
                distance_km=excluded.distance_km,
                steps=excluded.steps,
                calories_burned=excluded.calories_burned,
                transit_minutes=excluded.transit_minutes
            """,
            (user_id, request.idempotency_key, request.class_id, request.class_code, request.class_name, scheduled.isoformat(), actual.isoformat(), int(was_on_time), request.mode, request.bus_line, request.distance_km, request.steps, request.calories_burned, request.transit_minutes, now),
        )
        row = connection.execute("SELECT * FROM trips WHERE user_id = ? AND idempotency_key = ?", (user_id, request.idempotency_key)).fetchone()
    return _trip_row_to_response(row)


def list_trips(user_id: int, from_date: Optional[str], to_date: Optional[str]) -> List[TripResponse]:
    clauses = ["user_id = ?"]
    params: list = [user_id]
    if from_date:
        clauses.append("date(actual_arrival_time) >= date(?)")
        params.append(from_date)
    if to_date:
        clauses.append("date(actual_arrival_time) <= date(?)")
        params.append(to_date)
    sql = "SELECT * FROM trips WHERE " + " AND ".join(clauses) + " ORDER BY actual_arrival_time DESC LIMIT 100"
    with _db() as connection:
        rows = connection.execute(sql, params).fetchall()
    return [_trip_row_to_response(row) for row in rows]


def _period_start(period: str, now: datetime) -> datetime:
    today = now.date()
    if period == "today":
        return datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    if period == "semester":
        month = 1 if now.month <= 5 else 6 if now.month <= 7 else 8
        return datetime(now.year, month, 1, tzinfo=timezone.utc)
    return datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)


def _streak_days(rows) -> int:
    dates = {_parse_iso(row["actual_arrival_time"]).date() for row in rows}
    cursor = datetime.now(timezone.utc).date()
    streak = 0
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def stats_summary(user_id: int, period: str) -> StatsSummary:
    now = datetime.now(timezone.utc)
    start = _period_start(period, now)
    with _db() as connection:
        rows = connection.execute("SELECT * FROM trips WHERE user_id = ? AND actual_arrival_time >= ? ORDER BY actual_arrival_time DESC", (user_id, start.isoformat())).fetchall()
    trip_count = len(rows)
    on_time_count = sum(1 for row in rows if row["was_on_time"])
    distance_km = round(sum(float(row["distance_km"] or 0) for row in rows), 2)
    steps = sum(int(row["steps"] or 0) for row in rows)
    calories = round(sum(float(row["calories_burned"] or 0) for row in rows), 2)
    transit_minutes = sum(int(row["transit_minutes"] or 0) for row in rows)
    return StatsSummary(
        trip_count=trip_count,
        on_time_count=on_time_count,
        on_time_rate=round(on_time_count / trip_count, 3) if trip_count else 0.0,
        streak_days=_streak_days(rows),
        distance_km=distance_km,
        steps=steps,
        calories_burned=calories,
        co2_saved_grams=round(distance_km * CO2_GRAMS_PER_KM_CAR),
        transit_minutes=transit_minutes,
    )
