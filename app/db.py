"""SQLite connection handling and schema.

Every connection runs in autocommit mode; writes that must be atomic go
through `transaction()`, which takes the write lock up front (BEGIN IMMEDIATE)
so two callers can never read the same "free" slot and both write it.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS clinic (
    id            TEXT PRIMARY KEY,
    name_en       TEXT NOT NULL,
    name_hi       TEXT NOT NULL,
    address_en    TEXT NOT NULL,
    address_hi    TEXT NOT NULL,
    landmark      TEXT NOT NULL,
    hours_en      TEXT NOT NULL,
    hours_hi      TEXT NOT NULL,
    payment_modes TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doctors (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    name_hi      TEXT NOT NULL,
    aliases_json TEXT NOT NULL,          -- spoken / mis-transcribed variants
    specialty    TEXT NOT NULL,
    specialty_hi TEXT NOT NULL,
    fee_inr      INTEGER NOT NULL CHECK (fee_inr >= 0),
    slot_mins    INTEGER NOT NULL CHECK (slot_mins IN (10, 15, 20, 30)),
    weekly_json  TEXT NOT NULL,          -- {"Mon": [["09:30","13:00"], ...], ...}
    breaks_json  TEXT NOT NULL,          -- [["11:00","11:15"], ...]
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS doctor_leave (
    doctor_id TEXT NOT NULL REFERENCES doctors(id),
    date      TEXT NOT NULL,              -- YYYY-MM-DD
    PRIMARY KEY (doctor_id, date)
);

CREATE TABLE IF NOT EXISTS services (
    id       TEXT PRIMARY KEY,
    name_en  TEXT NOT NULL,
    name_hi  TEXT NOT NULL,
    fee_inr  INTEGER NOT NULL CHECK (fee_inr >= 0),
    prep_en  TEXT NOT NULL,
    prep_hi  TEXT NOT NULL,
    walk_in  INTEGER NOT NULL CHECK (walk_in IN (0, 1)),
    hours_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faqs (
    id        TEXT PRIMARY KEY,
    category  TEXT NOT NULL,
    keywords  TEXT NOT NULL,
    answer_en TEXT NOT NULL,
    answer_hi TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS patients (
    id             TEXT PRIMARY KEY,               -- PAT-1001
    name           TEXT NOT NULL,
    preferred_lang TEXT NOT NULL,                   -- a code from app/languages.py
    demo_contact   TEXT NOT NULL UNIQUE CHECK (demo_contact LIKE 'DEMO-IN-%')
);

CREATE TABLE IF NOT EXISTS appointments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ref              TEXT NOT NULL UNIQUE,          -- CC-1042
    doctor_id        TEXT NOT NULL REFERENCES doctors(id),
    patient_id       TEXT NOT NULL REFERENCES patients(id),
    date             TEXT NOT NULL,                 -- YYYY-MM-DD
    start_time       TEXT NOT NULL,                 -- HH:MM
    fee_inr          INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'CANCELLED', 'COMPLETED')),
    source           TEXT NOT NULL CHECK (source IN ('AI', 'STAFF', 'SEED')),
    cancel_reason    TEXT,
    rescheduled_from TEXT,                          -- ref of the booking this replaced
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- The guarantee against double-booking: at most one live booking per doctor slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot
    ON appointments (doctor_id, date, start_time)
    WHERE status IN ('CONFIRMED', 'COMPLETED');

-- A patient cannot hold two live bookings at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_patient_time
    ON appointments (patient_id, date, start_time)
    WHERE status IN ('CONFIRMED', 'COMPLETED');

CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    patient_id TEXT REFERENCES patients(id),
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    language   TEXT,
    intent     TEXT,
    outcome    TEXT,
    escalated  INTEGER NOT NULL DEFAULT 0 CHECK (escalated IN (0, 1)),
    summary    TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id          INTEGER NOT NULL REFERENCES calls(id),
    role             TEXT NOT NULL CHECK (role IN ('user', 'bot')),
    text             TEXT NOT NULL,
    lang             TEXT,
    gnani_request_id TEXT,
    stt_ms           INTEGER,
    llm_ms           INTEGER,
    tts_ms           INTEGER,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         INTEGER REFERENCES calls(id),
    patient_id      TEXT REFERENCES patients(id),
    reason          TEXT NOT NULL CHECK (reason IN
                        ('MEDICAL', 'HUMAN_REQUEST', 'NOT_UNDERSTOOD', 'SYSTEM_ERROR', 'OTHER')),
    priority        TEXT NOT NULL CHECK (priority IN ('URGENT', 'NORMAL')),
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    trigger_text    TEXT NOT NULL,
    resolution_note TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);
"""

TABLES_IN_DROP_ORDER = ["followups", "turns", "calls", "appointments", "patients",
                        "faqs", "services", "doctor_leave", "doctors", "clinic"]


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic write block holding the database write lock from the start."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def create_schema(conn: sqlite3.Connection, drop_existing: bool = False) -> None:
    if drop_existing:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES_IN_DROP_ORDER:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
