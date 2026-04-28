from typing import Optional, List, Literal

from pydantic import BaseModel, Field


class Telemetry(BaseModel):
    bus_id: str
    crowding_percentage: float
    passenger_count: int
    seats_available: int
    timestamp: Optional[str] = None
    location: Optional[str] = None
    device_status: Optional[str] = None


class NextClass(BaseModel):
    course: str
    location: str
    start_time: str
    minutes_until: int


class NearestStop(BaseModel):
    stop_name: str
    walking_minutes: int


class AvailableBus(BaseModel):
    route: str
    arrival_in_min: int
    capacity: int
    seats_left: int
    current_stop: str
    recommendation: str


class NextTripResponse(BaseModel):
    next_class: NextClass
    nearest_stop: NearestStop
    available_buses: List[AvailableBus]
    recommendation: str
    confidence: float
    message: str


class CalendarEvent(BaseModel):
    id: str
    title: str
    location: str
    start_time: str
    end_time: Optional[str] = None
    is_course: bool = False
    course_code: Optional[str] = None


class CommuteOrigin(BaseModel):
    name: Optional[str] = None
    lat: float
    lng: float


class CalendarCommuteOption(BaseModel):
    type: str
    route_number: Optional[str] = None
    route_name: Optional[str] = None
    leave_by: str
    depart_at: Optional[str] = None
    arrive_at: str
    total_trip_time_minutes: int
    walk_time_minutes: int
    ride_time_minutes: int = 0
    destination_walk_time_minutes: int = 0
    boarding_stop_name: Optional[str] = None
    alighting_stop_name: Optional[str] = None


class CalendarCommutePreview(BaseModel):
    event_id: str
    origin_name: Optional[str] = None
    destination: str
    source: str
    leave_by: Optional[str] = None
    best_route_number: Optional[str] = None
    options: List[CalendarCommuteOption] = Field(default_factory=list)


class CalendarImportResponse(BaseModel):
    source: str
    events: List[CalendarEvent]
    commute_previews: Optional[List[CalendarCommutePreview]] = None


class AuthRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


class AuthResponse(BaseModel):
    token: str
    email: str
    name: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    token: str


class CalendarEventUpdateItem(BaseModel):
    external_ids: List[str]
    title: Optional[str] = None
    location: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class BulkCalendarEventUpdate(BaseModel):
    updates: List[CalendarEventUpdateItem]


class DeleteEventsRequest(BaseModel):
    external_ids: List[str]


class ManualEventsRequest(BaseModel):
    events: List[CalendarEvent]
    commute_origin: Optional[CommuteOrigin] = None


class StudentDiscount(BaseModel):
    percentage: Optional[float] = None
    details: Optional[str] = None
    valid_until: Optional[str] = None
    is_new: bool = False


class LocalEat(BaseModel):
    id: str
    name: str
    type: str
    emoji: str
    rating: Optional[float] = None
    price_level: Optional[str] = None
    distance_miles: Optional[float] = None
    walk_time_minutes: Optional[int] = None
    address: Optional[str] = None
    hours: Optional[List[str]] = None
    description: Optional[str] = None
    is_open: Optional[bool] = None
    is_favorite: bool = False
    cuisine_type: Optional[str] = None
    student_discount: Optional[StudentDiscount] = None


class LocalEatsResponse(BaseModel):
    eateries: List[LocalEat]
    source: str


class TripLogRequest(BaseModel):
    idempotency_key: str
    class_id: str
    class_code: str
    class_name: str
    scheduled_time: str
    actual_arrival_time: str
    mode: Literal["bus", "walk"]
    bus_line: Optional[str] = None
    distance_km: float = Field(ge=0)
    steps: Optional[int] = Field(default=None, ge=0)
    calories_burned: Optional[float] = Field(default=None, ge=0)
    transit_minutes: Optional[int] = Field(default=None, ge=0)


class TripResponse(BaseModel):
    id: int
    date: str
    class_code: str
    class_name: str
    scheduled_time: str
    actual_arrival_time: str
    was_on_time: bool
    mode: str
    bus_line: Optional[str] = None
    distance_km: float
    steps: Optional[int] = None
    calories_burned: Optional[float] = None
    transit_minutes: Optional[int] = None


class StatsSummary(BaseModel):
    trip_count: int
    on_time_count: int
    on_time_rate: float
    streak_days: int
    distance_km: float
    steps: int
    calories_burned: float
    co2_saved_grams: int
    transit_minutes: int


class UserPreferences(BaseModel):
    smart_departure_alerts: bool = True
    capacity_alerts: bool = True
    delay_notifications: bool = True
    theme_id: Optional[str] = None


class UserPreferencesPatch(BaseModel):
    smart_departure_alerts: Optional[bool] = None
    capacity_alerts: Optional[bool] = None
    delay_notifications: Optional[bool] = None
    theme_id: Optional[str] = None
