import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from config import DATABASE_PATH, DEFAULT_USER_PREFERENCES


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                email_verified INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                success INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS calendar_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                external_id TEXT,
                title TEXT NOT NULL,
                location TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS local_eats (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                type TEXT NOT NULL,
                emoji TEXT NOT NULL,
                rating REAL,
                price_level TEXT,
                lat REAL,
                lng REAL,
                address TEXT,
                hours_json TEXT,
                description TEXT,
                is_open INTEGER,
                cuisine_type TEXT,
                student_discount_json TEXT,
                last_verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_eat_favorites (
                user_id INTEGER NOT NULL,
                eatery_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, eatery_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(eatery_id) REFERENCES local_eats(id)
            );
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                class_id TEXT NOT NULL,
                class_code TEXT NOT NULL,
                class_name TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                actual_arrival_time TEXT NOT NULL,
                was_on_time INTEGER NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('bus', 'walk')),
                bus_line TEXT,
                distance_km REAL NOT NULL,
                steps INTEGER,
                calories_burned REAL,
                transit_minutes INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, idempotency_key),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                smart_departure_alerts INTEGER NOT NULL DEFAULT 1,
                capacity_alerts INTEGER NOT NULL DEFAULT 1,
                delay_notifications INTEGER NOT NULL DEFAULT 1,
                theme_id TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            """
        )


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _json_or_none(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preference_defaults() -> dict[str, Any]:
    return dict(DEFAULT_USER_PREFERENCES)
