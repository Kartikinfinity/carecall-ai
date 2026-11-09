"""Appointment slots and atomic booking operations.

Slots are computed from each doctor's weekly schedule rather than stored, so
availability is always: schedule - breaks - leave - live bookings - the past.
Double-booking is prevented by a partial unique index (see db.py); the checks
here give friendly errors, the index is the final guarantee.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import IST, now_ist
from .db import transaction

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# A slot must start at least this long after "now" to be offered or booked.
BOOKING_LEAD = timedelta(minutes=10)
LIVE = ("CONFIRMED", "COMPLETED")
PARTS_OF_DAY = {"morning": ("00:00", "12:00"), "afternoon": ("12:00", "16:00"), "evening": ("16:00", "24:00")}


class BookingError(Exception):
    """Base class; `code` is a stable machine-readable reason for the dialog layer."""
    code = "BOOKING_ERROR"


class NotFound(BookingError):
    code = "NOT_FOUND"


class InvalidSlot(BookingError):
    code = "INVALID_SLOT"


class SlotUnavailable(BookingError):
    code = "SLOT_UNAVAILABLE"


class InvalidState(BookingError):
    code = "INVALID_STATE"


@dataclass(frozen=True)
class Slot:
    doctor_id: str
    date: str        # YYYY-MM-DD
    start: str       # HH:MM
    end: str         # HH:MM

    def as_dict(self) -> dict:
        return {"doctor_id": self.doctor_id, "date": self.date, "start": self.start, "end": self.end}


# --------------------------------------------------------------------------- helpers

def _t(hhmm: str) -> time:
    return datetime.strptime(hhmm, "%H:%M").time()


def _dt(day: str, hhmm: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), _t(hhmm), tzinfo=IST)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def get_doctor(conn: sqlite3.Connection, doctor_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
    if row is None:
        raise NotFound(f"No doctor {doctor_id}")
    return row


def on_leave(conn: sqlite3.Connection, doctor_id: str, day: str) -> bool:
    return conn.execute("SELECT 1 FROM doctor_leave WHERE doctor_id = ? AND date = ?",
                        (doctor_id, day)).fetchone() is not None


# --------------------------------------------------------------------------- slots

def schedule_slots(doctor: sqlite3.Row, day: str) -> list[Slot]:
    """Every slot the doctor's weekly schedule produces on `day`, ignoring bookings."""
    weekday = WEEKDAYS[date.fromisoformat(day).weekday()]
    windows = json.loads(doctor["weekly_json"]).get(weekday, [])
    breaks = [(_dt(day, a), _dt(day, b)) for a, b in json.loads(doctor["breaks_json"])]
    step = timedelta(minutes=doctor["slot_mins"])
    slots: list[Slot] = []
    for start_s, end_s in windows:
        cur, end = _dt(day, start_s), _dt(day, end_s)
        while cur + step <= end:
            nxt = cur + step
            if not any(cur < b_end and nxt > b_start for b_start, b_end in breaks):
                slots.append(Slot(doctor["id"], day, cur.strftime("%H:%M"), nxt.strftime("%H:%M")))
            cur = nxt
    return slots


def booked_starts(conn: sqlite3.Connection, doctor_id: str, day: str) -> set[str]:
    rows = conn.execute(
        "SELECT start_time FROM appointments WHERE doctor_id = ? AND date = ? AND status IN (?, ?)",
        (doctor_id, day, *LIVE))
    return {r["start_time"] for r in rows}


def available_slots(conn: sqlite3.Connection, doctor_id: str, day: str,
                    now: datetime | None = None) -> list[Slot]:
    now = now or now_ist()
    doctor = get_doctor(conn, doctor_id)
    if not doctor["active"] or on_leave(conn, doctor_id, day):
        return []
    taken = booked_starts(conn, doctor_id, day)
    earliest = now + BOOKING_LEAD
    return [s for s in schedule_slots(doctor, day)
            if s.start not in taken and _dt(day, s.start) >= earliest]


def find_slots(conn: sqlite3.Connection, doctor_id: str, day: str | None = None,
               preferred_time: str | None = None, part_of_day: str | None = None,
               limit: int = 2, days_ahead: int = 7, now: datetime | None = None) -> list[Slot]:
    """Nearest open slots, searching forward from `day` (default today).

    preferred_time ("HH:MM") orders the first matching day's slots by closeness
    to that time; part_of_day filters: morning < 12:00 <= afternoon < 16:00 <= evening.
    """
    now = now or now_ist()
    start_day = date.fromisoformat(day) if day else now.date()
    found: list[Slot] = []
    window = PARTS_OF_DAY.get(part_of_day or "")
    for offset in range(days_ahead + 1):
        d = (start_day + timedelta(days=offset)).isoformat()
        slots = available_slots(conn, doctor_id, d, now)
        if window:
            slots = [s for s in slots if window[0] <= s.start < window[1]]
        if preferred_time and offset == 0:
            target = _dt(d, preferred_time)
            slots.sort(key=lambda s: abs((_dt(d, s.start) - target).total_seconds()))
        found.extend(slots)
        if len(found) >= limit:
            break
    return found[:limit]


def count_open_slots(conn: sqlite3.Connection, day: str, now: datetime | None = None) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM doctors")]
    return sum(len(available_slots(conn, i, day, now)) for i in ids)


# --------------------------------------------------------------------------- lookups

_TITLE = re.compile(r"\b(dr|doctor|doc|sir|madam|ma'am|ji)\b\.?|डॉक्टर|डॉ\.?|डा\.|जी", re.I)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text.lower())
    text = _TITLE.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_doctor(conn: sqlite3.Connection, query: str, cutoff: float = 0.72) -> sqlite3.Row | None:
    """Match a spoken doctor name or specialty, tolerant of transcription variants."""
    q = _norm(query)
    if not q:
        return None
    best, best_score = None, 0.0
    for doc in conn.execute("SELECT * FROM doctors"):
        names = [doc["name"], doc["name_hi"], doc["specialty"], doc["specialty_hi"],
                 *json.loads(doc["aliases_json"])]
        for name in names:
            n = _norm(name)
            if not n:
                continue
            if q == n or (len(q) >= 4 and (q in n.split() or n in q)):
                score = 1.0
            else:
                # Compare against each word too, so "Chandrakar" matches "Aarav Chandrakar".
                score = max([difflib.SequenceMatcher(None, q, n).ratio()] +
                            [difflib.SequenceMatcher(None, q, w).ratio() for w in n.split()])
            if score > best_score:
                best, best_score = doc, score
    return best if best_score >= cutoff else None


_REF = re.compile(r"(?:c\s*c)?\s*[-–]?\s*(\d{4})", re.I)


def normalize_ref(text: str) -> str | None:
    """'CC-1042', 'cc 1042', '1042' -> 'CC-1042'."""
    m = _REF.search(text.replace(" ", " "))
    return f"CC-{m.group(1)}" if m else None


def get_appointment(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    norm = normalize_ref(ref) or ref
    row = conn.execute("SELECT * FROM appointments WHERE ref = ?", (norm,)).fetchone()
    if row is None:
        raise NotFound(f"No appointment {ref}")
    return row


def upcoming_for_patient(conn: sqlite3.Connection, patient_id: str,
                         now: datetime | None = None) -> list[sqlite3.Row]:
    now = now or now_ist()
    rows = conn.execute(
        "SELECT a.*, d.name AS doctor_name FROM appointments a JOIN doctors d ON d.id = a.doctor_id "
        "WHERE a.patient_id = ? AND a.status = 'CONFIRMED' ORDER BY a.date, a.start_time",
        (patient_id,)).fetchall()
    return [r for r in rows if _dt(r["date"], r["start_time"]) >= now]


def create_patient(conn: sqlite3.Connection, name: str, preferred_lang: str = "hi-en") -> str:
    with transaction(conn):
        n = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0] + 1
        while True:
            pid, contact = f"PAT-{1000 + n}", f"DEMO-IN-{n:04d}"
            if conn.execute("SELECT 1 FROM patients WHERE id = ? OR demo_contact = ?",
                            (pid, contact)).fetchone() is None:
                break
            n += 1
        conn.execute("INSERT INTO patients (id, name, preferred_lang, demo_contact) VALUES (?, ?, ?, ?)",
                     (pid, name.strip(), preferred_lang, contact))
    return pid


# --------------------------------------------------------------------------- writes

def _next_ref(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(ref, 4) AS INTEGER)) FROM appointments").fetchone()
    return f"CC-{(row[0] or 1000) + 1}"


def _validate_slot(conn: sqlite3.Connection, doctor: sqlite3.Row, day: str, start: str,
                   now: datetime) -> None:
    try:
        date.fromisoformat(day)
        _t(start)
    except ValueError as exc:
        raise InvalidSlot(f"Bad date/time {day} {start}") from exc
    if not doctor["active"] or on_leave(conn, doctor["id"], day):
        raise SlotUnavailable(f"{doctor['name']} is not available on {day}")
    if start not in {s.start for s in schedule_slots(doctor, day)}:
        raise InvalidSlot(f"{start} on {day} is not a slot in {doctor['name']}'s schedule")
    if _dt(day, start) < now + BOOKING_LEAD:
        raise InvalidSlot(f"{day} {start} is in the past or too soon")


def _insert(conn, doctor, patient_id, day, start, source, now, rescheduled_from=None) -> str:
    ref = _next_ref(conn)
    try:
        conn.execute(
            "INSERT INTO appointments (ref, doctor_id, patient_id, date, start_time, fee_inr, status,"
            " source, rescheduled_from, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'CONFIRMED', ?, ?, ?, ?)",
            (ref, doctor["id"], patient_id, day, start, doctor["fee_inr"], source,
             rescheduled_from, _iso(now), _iso(now)))
    except sqlite3.IntegrityError as exc:
        msg = str(exc)
        if "patient_id" in msg:
            raise SlotUnavailable("Patient already has a booking at that time") from exc
        if "FOREIGN KEY" in msg:
            raise NotFound("Unknown patient or doctor") from exc
        raise SlotUnavailable(f"{day} {start} was just taken") from exc
    return ref


def book(conn: sqlite3.Connection, doctor_id: str, patient_id: str, day: str, start: str,
         source: str = "AI", now: datetime | None = None) -> sqlite3.Row:
    """Atomically book one slot. Raises SlotUnavailable if someone got there first."""
    now = now or now_ist()
    with transaction(conn):
        doctor = get_doctor(conn, doctor_id)
        _validate_slot(conn, doctor, day, start, now)
        ref = _insert(conn, doctor, patient_id, day, start, source, now)
    return get_appointment(conn, ref)


def cancel(conn: sqlite3.Connection, ref: str, reason: str,
           now: datetime | None = None) -> sqlite3.Row:
    now = now or now_ist()
    with transaction(conn):
        appt = get_appointment(conn, ref)
        if appt["status"] != "CONFIRMED":
            raise InvalidState(f"{appt['ref']} is {appt['status'].lower()}, not confirmed")
        conn.execute("UPDATE appointments SET status = 'CANCELLED', cancel_reason = ?, updated_at = ?"
                     " WHERE id = ?", (reason.strip() or "Cancelled", _iso(now), appt["id"]))
    return get_appointment(conn, appt["ref"])


def reschedule(conn: sqlite3.Connection, ref: str, new_day: str, new_start: str,
               new_doctor_id: str | None = None, now: datetime | None = None) -> sqlite3.Row:
    """Move a booking in one transaction: the old one is cancelled only if the new one is booked."""
    now = now or now_ist()
    with transaction(conn):
        old = get_appointment(conn, ref)
        if old["status"] != "CONFIRMED":
            raise InvalidState(f"{old['ref']} is {old['status'].lower()}, not confirmed")
        doctor = get_doctor(conn, new_doctor_id or old["doctor_id"])
        if (doctor["id"], new_day, new_start) == (old["doctor_id"], old["date"], old["start_time"]):
            raise InvalidSlot("That is the current appointment time")
        _validate_slot(conn, doctor, new_day, new_start, now)
        # Free the old slot first so the patient-time index doesn't block a same-time move.
        conn.execute("UPDATE appointments SET status = 'CANCELLED', updated_at = ? WHERE id = ?",
                     (_iso(now), old["id"]))
        new_ref = _insert(conn, doctor, old["patient_id"], new_day, new_start, "AI", now,
                          rescheduled_from=old["ref"])
        conn.execute("UPDATE appointments SET cancel_reason = ? WHERE id = ?",
                     (f"Rescheduled to {new_ref}", old["id"]))
    return get_appointment(conn, new_ref)


def set_leave(conn: sqlite3.Connection, doctor_id: str, day: str, on: bool) -> None:
    with transaction(conn):
        get_doctor(conn, doctor_id)
        if on:
            conn.execute("INSERT OR IGNORE INTO doctor_leave (doctor_id, date) VALUES (?, ?)",
                         (doctor_id, day))
        else:
            conn.execute("DELETE FROM doctor_leave WHERE doctor_id = ? AND date = ?", (doctor_id, day))
