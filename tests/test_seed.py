import re
from datetime import datetime, timedelta

from app import booking, db, seed
from app.config import IST

from .conftest import MONDAY_8AM


def test_seed_counts(conn):
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("doctors", "services", "faqs", "patients", "calls", "followups")}
    assert counts == {"doctors": 4, "services": 6, "faqs": 10, "patients": 20, "calls": 3, "followups": 2}
    statuses = dict(conn.execute("SELECT status, COUNT(*) FROM appointments GROUP BY status").fetchall())
    assert statuses.get("CONFIRMED", 0) >= 12
    assert statuses.get("CANCELLED") == 1


def test_seeded_bookings_sit_on_real_schedule_slots(conn):
    for appt in conn.execute("SELECT * FROM appointments"):
        doctor = booking.get_doctor(conn, appt["doctor_id"])
        starts = {s.start for s in booking.schedule_slots(doctor, appt["date"])}
        assert appt["start_time"] in starts, dict(appt)
        assert appt["fee_inr"] == doctor["fee_inr"]


def test_seeded_dates_are_relative_to_now(conn):
    dates = [r[0] for r in conn.execute("SELECT date FROM appointments WHERE status = 'CONFIRMED'")]
    assert min(dates) >= MONDAY_8AM.date().isoformat()
    assert max(dates) <= (MONDAY_8AM.date() + timedelta(days=8)).isoformat()


def test_seed_on_a_sunday_still_produces_future_bookings(db_path):
    c = db.connect(db_path)
    sunday = datetime(2026, 11, 15, 10, 0, tzinfo=IST)
    seed.seed(c, now=sunday)
    dates = [r[0] for r in c.execute("SELECT date FROM appointments")]
    assert dates and all(d > "2026-11-15" for d in dates)
    c.close()


def test_seed_midday_marks_past_slots_completed(db_path):
    c = db.connect(db_path)
    noon = MONDAY_8AM.replace(hour=12)
    seed.seed(c, now=noon)
    done = c.execute("SELECT date, start_time FROM appointments WHERE status = 'COMPLETED'").fetchall()
    assert len(done) == 3 and all(f"{d} {t}" < "2026-11-16 12:00" for d, t in done)
    c.close()


def test_reseed_is_idempotent(conn):
    first = seed.seed(conn, now=MONDAY_8AM)
    second = seed.seed(conn, now=MONDAY_8AM)
    assert first == second


def test_no_dialable_numbers_or_ids_anywhere(conn):
    """Contest rule: no real phone, Aadhaar or PAN numbers in the data."""
    phone = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
    aadhaar = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")
    pan = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            for value in row:
                if isinstance(value, str):
                    assert not phone.search(value), (table, value)
                    assert not aadhaar.search(value), (table, value)
                    assert not pan.search(value), (table, value)
    for (contact,) in conn.execute("SELECT demo_contact FROM patients"):
        assert re.fullmatch(r"DEMO-IN-\d{4}", contact)
