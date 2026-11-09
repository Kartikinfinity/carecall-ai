import threading
from datetime import timedelta

import pytest

from app import booking, db

from .conftest import MONDAY_8AM

MON = "2026-11-16"
WED = "2026-11-18"
SUN = "2026-11-22"
NOW = MONDAY_8AM


def starts(conn, doctor_id, day, now=NOW):
    return [s.start for s in booking.available_slots(conn, doctor_id, day, now)]


# ---------------------------------------------------------------- slot computation

def test_schedule_respects_windows_and_breaks(conn):
    doc = booking.get_doctor(conn, "DOC-101")   # 15-min slots, breaks 11:00-11:15, 18:30-18:45
    s = [x.start for x in booking.schedule_slots(doc, MON)]
    assert s[0] == "09:30" and "12:45" in s and "13:00" not in s
    assert "11:00" not in s and "11:15" in s
    assert "17:00" in s and "18:30" not in s and "18:45" in s and "19:45" in s and "20:00" not in s


def test_slot_must_fit_entirely_inside_window(conn):
    doc = booking.get_doctor(conn, "DOC-102")   # 20-min slots, evening 17:00-19:40
    s = [x.start for x in booking.schedule_slots(doc, MON)]
    assert "19:20" in s and "19:40" not in s
    assert "11:20" not in s and "11:40" in s   # break 11:20-11:40


def test_closed_days(conn):
    assert starts(conn, "DOC-101", SUN) == []           # Sunday
    assert starts(conn, "DOC-103", WED) == []           # Dr. Deshmukh's weekly off
    assert starts(conn, "DOC-104", MON)[-1] < "13:30"   # ultrasound has no evening session


def test_past_and_imminent_slots_excluded(conn):
    at_1005 = NOW.replace(hour=10, minute=5)
    s = starts(conn, "DOC-101", MON, now=at_1005)
    assert s and min(s) >= "10:15"                      # 10-minute lead time


def test_leave_and_inactive_doctor_have_no_slots(conn):
    booking.set_leave(conn, "DOC-101", MON, True)
    assert starts(conn, "DOC-101", MON) == []
    booking.set_leave(conn, "DOC-101", MON, False)
    assert starts(conn, "DOC-101", MON)
    conn.execute("UPDATE doctors SET active = 0 WHERE id = 'DOC-102'")
    assert starts(conn, "DOC-102", MON) == []


def test_find_slots_prefers_requested_time_and_skips_closed_days(conn):
    near = booking.find_slots(conn, "DOC-101", MON, preferred_time="10:30", now=NOW)
    assert [s.start for s in near] == ["10:30", "10:15"]   # exact match first, then nearest
    # Asking for Dr. Deshmukh on his day off rolls forward to Thursday.
    rolled = booking.find_slots(conn, "DOC-103", WED, now=NOW)
    assert rolled and rolled[0].date == "2026-11-19"
    evening = booking.find_slots(conn, "DOC-101", MON, part_of_day="evening", now=NOW)
    assert evening and all(s.start >= "16:00" for s in evening)
    morning = booking.find_slots(conn, "DOC-101", MON, part_of_day="morning", now=NOW)
    assert morning and all(s.start < "12:00" for s in morning)


# ---------------------------------------------------------------- booking

def test_book_then_slot_disappears(conn):
    appt = booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)
    assert appt["status"] == "CONFIRMED" and appt["ref"].startswith("CC-") and appt["fee_inr"] == 400
    assert "10:30" not in starts(conn, "DOC-101", MON)
    with pytest.raises(booking.SlotUnavailable):
        booking.book(conn, "DOC-101", "PAT-1018", MON, "10:30", now=NOW)


@pytest.mark.parametrize("day,start,error", [
    (MON, "10:07", booking.InvalidSlot),        # not on the 15-minute grid
    (MON, "11:00", booking.InvalidSlot),        # break
    (SUN, "10:00", booking.InvalidSlot),        # closed
    ("2026-11-13", "10:00", booking.InvalidSlot),  # in the past
    ("16/11/2026", "10:00", booking.InvalidSlot),  # malformed date
])
def test_invalid_bookings_rejected(conn, day, start, error):
    with pytest.raises(error):
        booking.book(conn, "DOC-101", "PAT-1020", day, start, now=NOW)


def test_booking_on_leave_rejected(conn):
    booking.set_leave(conn, "DOC-101", MON, True)
    with pytest.raises(booking.SlotUnavailable):
        booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)


def test_patient_cannot_hold_two_bookings_at_same_time(conn):
    booking.book(conn, "DOC-101", "PAT-1020", MON, "10:00", now=NOW)
    with pytest.raises(booking.SlotUnavailable):
        booking.book(conn, "DOC-102", "PAT-1020", MON, "10:00", now=NOW)


def test_unknown_patient_or_doctor(conn):
    with pytest.raises(booking.NotFound):
        booking.book(conn, "DOC-999", "PAT-1020", MON, "10:00", now=NOW)
    with pytest.raises(booking.NotFound):
        booking.book(conn, "DOC-101", "PAT-9999", MON, "10:00", now=NOW)


def test_concurrent_bookings_for_one_slot_exactly_one_wins(conn, db_path):
    """Ten callers race for the same slot on separate connections."""
    results, barrier = [], threading.Barrier(10)
    patients = [f"PAT-10{i:02d}" for i in range(1, 11)]

    def attempt(pid):
        c = db.connect(db_path)
        try:
            barrier.wait()
            booking.book(c, "DOC-104", pid, MON, "12:30", now=NOW)
            results.append("ok")
        except booking.SlotUnavailable:
            results.append("taken")
        except Exception as exc:          # anything else (e.g. "database is locked") is a failure
            results.append(f"error: {exc!r}")
        finally:
            c.close()

    threads = [threading.Thread(target=attempt, args=(p,)) for p in patients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1, results
    assert results.count("taken") == 9, results
    live = conn.execute("SELECT COUNT(*) FROM appointments WHERE doctor_id = 'DOC-104' AND date = ?"
                        " AND start_time = '12:30' AND status = 'CONFIRMED'", (MON,)).fetchone()[0]
    assert live == 1


# ---------------------------------------------------------------- cancel / reschedule

def test_cancel_frees_slot_and_keeps_record(conn):
    appt = booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)
    done = booking.cancel(conn, appt["ref"].lower().replace("-", " "), "Patient request", now=NOW)
    assert done["status"] == "CANCELLED" and done["cancel_reason"] == "Patient request"
    assert "10:30" in starts(conn, "DOC-101", MON)
    with pytest.raises(booking.InvalidState):
        booking.cancel(conn, appt["ref"], "again", now=NOW)
    # The freed slot can be booked by someone else.
    assert booking.book(conn, "DOC-101", "PAT-1018", MON, "10:30", now=NOW)["status"] == "CONFIRMED"


def test_reschedule_moves_booking_atomically(conn):
    old = booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)
    new = booking.reschedule(conn, old["ref"], MON, "12:00", now=NOW)
    old_row = booking.get_appointment(conn, old["ref"])
    assert new["status"] == "CONFIRMED" and new["rescheduled_from"] == old["ref"]
    assert old_row["status"] == "CANCELLED" and old_row["cancel_reason"] == f"Rescheduled to {new['ref']}"
    free = starts(conn, "DOC-101", MON)
    assert "10:30" in free and "12:00" not in free


def test_failed_reschedule_leaves_original_untouched(conn):
    old = booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)
    booking.book(conn, "DOC-101", "PAT-1018", MON, "12:00", now=NOW)
    with pytest.raises(booking.SlotUnavailable):
        booking.reschedule(conn, old["ref"], MON, "12:00", now=NOW)
    assert booking.get_appointment(conn, old["ref"])["status"] == "CONFIRMED"
    assert "10:30" not in starts(conn, "DOC-101", MON)


def test_reschedule_to_another_doctor_and_same_time(conn):
    old = booking.book(conn, "DOC-101", "PAT-1020", MON, "10:00", now=NOW)
    new = booking.reschedule(conn, old["ref"], MON, "10:00", new_doctor_id="DOC-102", now=NOW)
    assert new["doctor_id"] == "DOC-102" and new["fee_inr"] == 500
    with pytest.raises(booking.InvalidSlot):
        booking.reschedule(conn, new["ref"], MON, "10:00", new_doctor_id="DOC-102", now=NOW)


def test_upcoming_for_patient(conn):
    booking.book(conn, "DOC-101", "PAT-1020", MON, "10:30", now=NOW)
    upcoming = booking.upcoming_for_patient(conn, "PAT-1020", now=NOW)
    assert [(a["date"], a["start_time"]) for a in upcoming] == [(MON, "10:30")]
    assert upcoming[0]["doctor_name"] == "Dr. Aarav Chandrakar"
    assert booking.upcoming_for_patient(conn, "PAT-1020", now=NOW + timedelta(days=2)) == []


def test_new_patient_gets_synthetic_ids(conn):
    pid = booking.create_patient(conn, "Test Caller")
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    assert pid == "PAT-1021" and row["demo_contact"] == "DEMO-IN-0021"


# ---------------------------------------------------------------- lookups

@pytest.mark.parametrize("spoken,expected", [
    ("Dr. Chandrakar", "DOC-101"), ("chandrakar sir", "DOC-101"), ("Aarav Chandrakar", "DOC-101"),
    ("डॉक्टर चांद्रकार", "DOC-101"), ("चंद्राकर", "DOC-101"), ("Chandrakaar", "DOC-101"),
    ("Meera Baghel", "DOC-102"), ("pediatrician", "DOC-102"), ("बच्चों के डॉक्टर", "DOC-102"),
    ("haddi doctor", "DOC-103"), ("Deshmukh", "DOC-103"), ("Orthopaedics", "DOC-103"),
    ("ultrasound", "DOC-104"), ("डॉ. साहू", "DOC-104"),
])
def test_find_doctor_tolerates_transcription_variants(conn, spoken, expected):
    doc = booking.find_doctor(conn, spoken)
    assert doc is not None and doc["id"] == expected


@pytest.mark.parametrize("spoken", ["Dr. Sharma", "dentist", "", "doctor"])
def test_find_doctor_rejects_unknown(conn, spoken):
    assert booking.find_doctor(conn, spoken) is None


@pytest.mark.parametrize("text,ref", [
    ("CC-1042", "CC-1042"), ("cc 1042", "CC-1042"), ("1042", "CC-1042"), ("C C 1042", "CC-1042"),
    ("मेरा booking number 1042 है", "CC-1042"), ("no number", None),
])
def test_normalize_ref(text, ref):
    assert booking.normalize_ref(text) == ref


def test_open_slot_count(conn):
    assert booking.count_open_slots(conn, SUN, now=NOW) == 0
    assert booking.count_open_slots(conn, MON, now=NOW) > 40
