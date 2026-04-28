import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from fastapi import HTTPException

from common import _ac_transit_now, _get_stripped, _parse_departure, _route_label, _time_to_seconds, _try_int
from config import CALENDAR_COMMUTE_PREVIEW_EVENT_LIMIT, CALENDAR_COMMUTE_PREVIEW_STOP_LIMIT, CALENDAR_COMMUTE_PREVIEW_STOP_TIME_LIMIT
from database import _db
from google_service import _geocode_destination, _google_route, _google_transit_candidates
from gtfs_service import (
    _active_gtfs_service_ids,
    _build_transit_segment,
    _load_gtfs_routes_by_id,
    _load_gtfs_stop_times_by_stop,
    _load_gtfs_trips_by_id,
    _nearest_gtfs_stop_id,
    _nearest_gtfs_stops,
)
from schemas import CalendarCommuteOption, CalendarCommutePreview, CalendarEvent, CommuteOrigin

def _datetime_on_service_day(local_date: datetime, seconds_after_midnight: int) -> datetime:
    midnight = datetime.combine(local_date.date(), datetime.min.time(), tzinfo=local_date.tzinfo)
    return midnight + timedelta(seconds=seconds_after_midnight)


def _schedule_commute_preview_for_event(
    event: CalendarEvent,
    origin: Optional[CommuteOrigin],
) -> Optional[CalendarCommutePreview]:
    if not origin or not event.location:
        return None

    try:
        class_start = _ac_transit_now(_parse_departure(event.start_time))
        origin_point = {"lat": origin.lat, "lng": origin.lng}
        destination_point = _geocode_destination(event.location, origin_point)
        destination_stop_id = _nearest_gtfs_stop_id(destination_point)
        active_service_ids = _active_gtfs_service_ids(class_start)
        direct_walk = _google_route(origin_point, destination_point)
    except (HTTPException, requests.RequestException, KeyError, ValueError, TypeError) as exc:
        print(f"[commute_preview] setup failed for event {event.id}: {exc!r}")
        return CalendarCommutePreview(
            event_id=event.id,
            origin_name=origin.name,
            destination=event.location,
            source="unavailable",
            options=[],
        )

    class_seconds = _time_to_seconds(class_start.hour, class_start.minute, class_start.second)
    trips_by_id = _load_gtfs_trips_by_id()
    routes_by_id = _load_gtfs_routes_by_id()
    stop_times_by_stop = _load_gtfs_stop_times_by_stop()

    # Ask Google which transit routes run between origin and destination at class time.
    # This is the same call the live /plan-trip endpoint makes, but with a future departure
    # time so Google returns routes that actually run at 5:30 AM (or whenever the class is),
    # not whatever happens to be running right now.
    google_candidates = _google_transit_candidates(origin_point, destination_point, departure_time=class_start)

    # Build the set of origin stops: nearest GTFS stops + any boarding stops Google suggested.
    # This mirrors the live routing logic so we find the same stops Google would use.
    origin_stops = _nearest_gtfs_stops(origin_point, limit=CALENDAR_COMMUTE_PREVIEW_STOP_LIMIT)
    seen_stop_ids = {stop["stop_id"] for stop in origin_stops}
    for candidate in google_candidates:
        boarding_stop_id = str(candidate.get("boarding_stop_id") or "")
        boarding_point = candidate.get("boarding_stop_point") or {}
        if not boarding_stop_id or boarding_stop_id in seen_stop_ids:
            continue
        if boarding_point.get("lat") is None or boarding_point.get("lng") is None:
            continue
        origin_stops.append({
            "stop_id": boarding_stop_id,
            "stop_name": candidate.get("boarding_stop_name") or "Boarding stop",
            "lat": boarding_point["lat"],
            "lng": boarding_point["lng"],
        })
        seen_stop_ids.add(boarding_stop_id)

    # Index Google candidates by boarding stop so we can pass the right one as a hint
    # to _build_transit_segment, which uses it to prefer the correct route direction.
    google_by_boarding: Dict[str, List[dict]] = {}
    for candidate in google_candidates:
        sid = str(candidate.get("boarding_stop_id") or "")
        if sid:
            google_by_boarding.setdefault(sid, []).append(candidate)

    options: List[CalendarCommuteOption] = []

    for stop in origin_stops:
        try:
            walk_to_stop = _google_route(origin_point, {"lat": stop["lat"], "lng": stop["lng"]})
        except (HTTPException, requests.RequestException) as exc:
            print(f"[commute_preview] walk-to-stop failed for stop {stop.get('stop_id')}: {exc!r}")
            continue
        latest_boarding_seconds = class_seconds - walk_to_stop["duration_minutes"] * 60
        if latest_boarding_seconds <= 0:
            continue

        stop_candidates = google_by_boarding.get(stop["stop_id"], [])

        candidate_stop_times = [
            stop_time
            for stop_time in stop_times_by_stop.get(stop["stop_id"], [])
            if stop_time.get("departure_seconds") is not None
            and stop_time["departure_seconds"] <= latest_boarding_seconds
            and (trip := trips_by_id.get(stop_time["trip_id"])) is not None
            and _get_stripped(trip, "service_id") in active_service_ids
        ][-CALENDAR_COMMUTE_PREVIEW_STOP_TIME_LIMIT:]

        for stop_time in candidate_stop_times:
            trip = trips_by_id[stop_time["trip_id"]]
            route_id = _get_stripped(trip, "route_id") or ""
            route = routes_by_id.get(route_id, {})
            route_name = _route_label(route, route_id)
            direction_id = _try_int(trip.get("direction_id"))

            # Pick the Google candidate whose route matches this trip, falling back to the
            # first available candidate so _build_transit_segment gets a direction hint.
            google_candidate = next(
                (c for c in stop_candidates if (c.get("route_id") == route_id or c.get("route_short_name") == route_name)),
                stop_candidates[0] if stop_candidates else None,
            )

            segment = _build_transit_segment(
                route_name=route_name,
                trip_id=trip.get("trip_id"),
                prediction_direction_id=direction_id,
                boarding_stop_id=stop["stop_id"],
                destination_stop_id=destination_stop_id,
                destination_point=destination_point,
                google_candidate=google_candidate,
            )
            if not segment:
                continue

            alighting_stop = segment.get("alighting_stop") or {}
            try:
                walk_from_stop = _google_route(
                    {"lat": alighting_stop["lat"], "lng": alighting_stop["lng"]},
                    destination_point,
                )
            except (HTTPException, requests.RequestException, KeyError) as exc:
                print(f"[commute_preview] walk-from-stop failed for trip {trip.get('trip_id')}: {exc!r}")
                continue

            departure_seconds = stop_time["departure_seconds"]
            depart_at = _datetime_on_service_day(class_start, departure_seconds)
            ride_minutes = int(segment.get("ride_time_minutes") or 0)
            arrive_at = depart_at + timedelta(minutes=ride_minutes + walk_from_stop["duration_minutes"])
            if arrive_at > class_start:
                continue

            leave_by = depart_at - timedelta(minutes=walk_to_stop["duration_minutes"])
            if leave_by <= _ac_transit_now():
                continue

            total_trip_minutes = max(round((arrive_at - leave_by).total_seconds() / 60), 1)
            options.append(
                CalendarCommuteOption(
                    type="bus",
                    route_number=route_name,
                    route_name=segment.get("route_long_name") or f"Route {route_name}",
                    leave_by=leave_by.isoformat(),
                    depart_at=depart_at.isoformat(),
                    arrive_at=arrive_at.isoformat(),
                    total_trip_time_minutes=total_trip_minutes,
                    walk_time_minutes=walk_to_stop["duration_minutes"],
                    ride_time_minutes=ride_minutes,
                    destination_walk_time_minutes=walk_from_stop["duration_minutes"],
                    boarding_stop_name=stop["stop_name"],
                    alighting_stop_name=alighting_stop.get("stop_name"),
                )
            )

    deduped: Dict[tuple, CalendarCommuteOption] = {}
    for option in options:
        key = (
            option.type,
            option.route_number or "walk",
            option.boarding_stop_name or "",
            option.alighting_stop_name or "",
        )
        existing = deduped.get(key)
        if existing is None or option.leave_by > existing.leave_by:
            deduped[key] = option

    sorted_options = sorted(
        deduped.values(),
        key=lambda option: (option.leave_by, -option.total_trip_time_minutes),
        reverse=True,
    )[:5]
    best_option = sorted_options[0] if sorted_options else None
    return CalendarCommutePreview(
        event_id=event.id,
        origin_name=origin.name,
        destination=event.location,
        source="schedule" if sorted_options else "unavailable",
        leave_by=best_option.leave_by if best_option else None,
        best_route_number=best_option.route_number if best_option and best_option.type == "bus" else None,
        options=sorted_options,
    )


def _calendar_commute_previews(
    events: List[CalendarEvent],
    origin: Optional[CommuteOrigin],
) -> Optional[List[CalendarCommutePreview]]:
    if not origin:
        return None
    upcoming_events = sorted(
        [event for event in events if _is_future_event(event.start_time)],
        key=lambda event: event.start_time,
    )[:CALENDAR_COMMUTE_PREVIEW_EVENT_LIMIT]
    previews = [
        preview
        for event in upcoming_events
        if (preview := _schedule_commute_preview_for_event(event, origin)) is not None
    ]
    return previews or None

def _unfold_ics(content: str) -> List[str]:
    unfolded: List[str] = []
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _clean_ics_text(value: str) -> str:
    return (
        value
        .replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .strip()
    )


def _parse_ics_params(key: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for part in key.split(";")[1:]:
        if "=" not in part:
            continue
        param_key, param_value = part.split("=", 1)
        params[param_key.upper()] = param_value
    return params


def _parse_ics_datetime(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith("Z"):
        parsed = datetime.strptime(normalized, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    if "T" in normalized:
        parsed = datetime.strptime(normalized[:15], "%Y%m%dT%H%M%S")
        return parsed.isoformat()
    parsed = datetime.strptime(normalized[:8], "%Y%m%d")
    return parsed.isoformat()


def _parse_ics_datetime_object(value: str) -> datetime:
    if "-" in value:
        return datetime.fromisoformat(value)
    return datetime.fromisoformat(_parse_ics_datetime(value))


def _parse_rrule(value: str) -> Dict[str, str]:
    rule: Dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        key, rule_value = part.split("=", 1)
        rule[key.upper()] = rule_value
    return rule


def _align_datetime_for_compare(value: datetime, reference: datetime) -> datetime:
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def _event_duration(event: dict) -> timedelta:
    try:
        start = _parse_ics_datetime_object(event["start_time"])
        end = _parse_ics_datetime_object(event.get("end_time") or event["start_time"])
    except (KeyError, ValueError):
        return timedelta(hours=1)
    duration = end - start
    return duration if duration.total_seconds() > 0 else timedelta(hours=1)


def _serialize_ics_datetime(value: datetime) -> str:
    return value.isoformat()


def _expand_recurring_event(event: dict) -> List[dict]:
    rrule = event.get("rrule")
    if not rrule:
        return [event]

    rule = _parse_rrule(rrule)
    if rule.get("FREQ") != "WEEKLY":
        return [event]

    try:
        base_start = _parse_ics_datetime_object(event["start_time"])
    except (KeyError, ValueError):
        return [event]

    duration = _event_duration(event)
    now = datetime.now(base_start.tzinfo or timezone.utc)
    until_raw = rule.get("UNTIL")
    count_raw = rule.get("COUNT")
    try:
        interval = max(int(rule.get("INTERVAL", "1") or "1"), 1)
    except ValueError:
        interval = 1
    until = None
    if until_raw:
        try:
            until = _align_datetime_for_compare(_parse_ics_datetime_object(until_raw), base_start)
        except ValueError:
            until = None

    byday = {
        "MO": 0,
        "TU": 1,
        "WE": 2,
        "TH": 3,
        "FR": 4,
        "SA": 5,
        "SU": 6,
    }
    target_weekdays = [
        byday[item]
        for item in (rule.get("BYDAY") or "").split(",")
        if item in byday
    ] or [base_start.weekday()]

    max_count = int(count_raw) if count_raw and count_raw.isdigit() else 260
    horizon = now + timedelta(days=210)
    occurrences: List[dict] = []
    base_week_start = base_start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=base_start.weekday())
    cursor_week_start = base_week_start
    excluded_starts = set(event.get("exdate") or [])
    generated = 0

    while generated < max_count and cursor_week_start <= horizon:
        week_delta = (cursor_week_start - base_week_start).days // 7
        if week_delta % interval != 0:
            cursor_week_start += timedelta(days=7)
            continue

        for weekday in target_weekdays:
            occurrence_start = cursor_week_start + timedelta(
                days=weekday,
                hours=base_start.hour,
                minutes=base_start.minute,
                seconds=base_start.second,
                microseconds=base_start.microsecond,
            )
            if occurrence_start < base_start:
                continue
            if until and occurrence_start > until:
                continue
            if occurrence_start.isoformat() in excluded_starts:
                continue
            generated += 1
            if occurrence_start + duration < now:
                continue
            occurrence = dict(event)
            occurrence["start_time"] = _serialize_ics_datetime(occurrence_start)
            occurrence["end_time"] = _serialize_ics_datetime(occurrence_start + duration)
            occurrence["uid"] = f"{event.get('uid') or event.get('summary')}-{occurrence_start.isoformat()}"
            occurrences.append(occurrence)
            if len(occurrences) >= 80:
                return occurrences

        cursor_week_start += timedelta(days=7)

    return occurrences or [event]


def _parse_ics_events(content: str) -> List[CalendarEvent]:
    events: List[CalendarEvent] = []
    current: Optional[dict] = None

    for raw_line in _unfold_ics(content):
        if raw_line == "BEGIN:VEVENT":
            current = {}
            continue
        if raw_line == "END:VEVENT":
            if current and not current.get("invalid_datetime") and current.get("summary") and current.get("start_time"):
                for occurrence in _expand_recurring_event(current):
                    events.append(
                        CalendarEvent(
                            id=occurrence.get("uid") or f"calendar_{len(events)}",
                            title=occurrence["summary"],
                            location=occurrence.get("location", ""),
                            start_time=occurrence["start_time"],
                            end_time=occurrence.get("end_time"),
                        )
                    )
            current = None
            continue
        if current is None or ":" not in raw_line:
            continue

        key, value = raw_line.split(":", 1)
        field = key.split(";", 1)[0].upper()
        params = _parse_ics_params(key)

        if field == "UID":
            current["uid"] = _clean_ics_text(value)
        elif field == "SUMMARY":
            current["summary"] = _clean_ics_text(value)
        elif field == "LOCATION":
            current["location"] = _clean_ics_text(value)
        elif field == "DTSTART":
            try:
                current["start_time"] = _parse_ics_datetime(value)
            except ValueError:
                current["invalid_datetime"] = True
        elif field == "DTEND":
            try:
                current["end_time"] = _parse_ics_datetime(value)
            except ValueError:
                current["end_time"] = None
        elif field == "RRULE":
            current["rrule"] = value.strip()
        elif field == "RECURRENCE-ID":
            try:
                current["recurrence_id"] = _parse_ics_datetime(value)
            except ValueError:
                current["recurrence_id"] = None
        elif field == "EXDATE":
            for item in value.split(","):
                if not item.strip():
                    continue
                try:
                    current.setdefault("exdate", []).append(_parse_ics_datetime(item))
                except ValueError:
                    continue

    return events


def _infer_course_code(title: str) -> str:
    match = re.search(r"\b([A-Z]{2,}(?:\s+[A-Z]{1,4})?)\s*[-:]?\s*([A-Z]?\d{1,4}[A-Z]?)\b", title.upper())
    if match:
        subject = re.sub(r"\s+", " ", match.group(1)).strip()
        return f"{subject} {match.group(2)}"
    return title[:12] or "CLASS"


def _extract_course_code(title: str) -> Optional[str]:
    normalized = title.upper().replace("\\", " ")
    known_courses = ["ENGIN 183C", "CDSS 170", "DATA 145"]
    compact_title = re.sub(r"[^A-Z0-9]", "", normalized)
    for course in known_courses:
        if re.sub(r"[^A-Z0-9]", "", course) in compact_title:
            return course
    match = re.search(r"\b([A-Z]{2,}(?:\s+[A-Z]{1,4})?)\s*[-:]?\s*([A-Z]?\d{1,4}[A-Z]?)\b", normalized)
    if not match:
        return None
    subject = re.sub(r"\s+", " ", match.group(1)).strip()
    return f"{subject} {match.group(2)}"


def _is_future_event(start_time: str) -> bool:
    try:
        parsed = datetime.fromisoformat(start_time)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _filter_and_annotate_calendar_events(events: List[CalendarEvent]) -> List[CalendarEvent]:
    filtered: List[CalendarEvent] = []
    for event in events:
        location = (event.location or "").strip()
        if not location:
            continue
        if not _is_future_event(event.start_time):
            continue
        course_code = _extract_course_code(event.title)
        is_course = course_code is not None
        filtered.append(
            CalendarEvent(
                id=event.id,
                title=event.title,
                location=location,
                start_time=event.start_time,
                end_time=event.end_time,
                is_course=is_course,
                course_code=course_code,
            )
        )
    return filtered


def _store_calendar_events(user_id: int, events: List[CalendarEvent], source: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as connection:
        connection.execute("DELETE FROM calendar_events WHERE user_id = ? AND source != 'manual'", (user_id,))
        connection.executemany(
            """
            INSERT INTO calendar_events
            (user_id, external_id, title, location, start_time, end_time, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (user_id, event.id, event.title, event.location, event.start_time, event.end_time, source, now)
                for event in events
            ],
        )
