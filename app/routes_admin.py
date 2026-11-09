"""Clinic admin API: today's overview, appointments, follow-ups, call log, doctors' leave, FAQs, demo reset."""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import booking, seed
from .booking import BookingError, _iso
from .config import now_ist
from .db import transaction
from .routes_voice import engine, get_conn

router = APIRouter(prefix="/api/admin")


class CancelIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=200)


class ResolveIn(BaseModel):
    note: str = Field(..., min_length=3, max_length=500)


class LeaveIn(BaseModel):
    date: date
    on: bool


class FaqIn(BaseModel):
    answer_en: str = Field(..., min_length=5, max_length=600)
    answer_hi: str = Field(..., min_length=5, max_length=600)
    active: bool = True


def _today() -> str:
    return now_ist().date().isoformat()


# ------------------------------------------------------------------ overview

@router.get("/summary")
def summary(conn=Depends(get_conn)) -> dict:
    today = _today()
    appts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM appointments WHERE date = ? GROUP BY status", (today,)).fetchall())
    calls_today = conn.execute(
        "SELECT COUNT(*), SUM(escalated) FROM calls WHERE substr(started_at, 1, 10) = ?", (today,)).fetchone()
    followups = dict(conn.execute(
        "SELECT priority, COUNT(*) FROM followups WHERE status = 'OPEN' GROUP BY priority").fetchall())
    return {
        "date": today,
        "appointments_today": appts.get("CONFIRMED", 0) + appts.get("COMPLETED", 0),
        "completed_today": appts.get("COMPLETED", 0),
        "open_slots_today": booking.count_open_slots(conn, today),
        "ai_calls_today": calls_today[0] or 0,
        "escalated_calls_today": calls_today[1] or 0,
        "open_followups": sum(followups.values()),
        "urgent_followups": followups.get("URGENT", 0),
    }


# ------------------------------------------------------------------ appointments

@router.get("/appointments")
def appointments(days: int = 7, include_cancelled: bool = False, conn=Depends(get_conn)) -> list[dict]:
    start = now_ist().date()
    end = (start + timedelta(days=max(0, min(days, 30)))).isoformat()
    statuses = ("CONFIRMED", "COMPLETED", "CANCELLED") if include_cancelled else ("CONFIRMED", "COMPLETED")
    rows = conn.execute(
        f"SELECT a.ref, a.date, a.start_time, a.status, a.source, a.fee_inr, a.cancel_reason, a.rescheduled_from,"
        f" a.updated_at, d.name AS doctor, d.specialty, p.name AS patient, p.demo_contact"
        f" FROM appointments a JOIN doctors d ON d.id = a.doctor_id JOIN patients p ON p.id = a.patient_id"
        f" WHERE a.date BETWEEN ? AND ? AND a.status IN ({','.join('?' * len(statuses))})"
        f" ORDER BY a.date, a.start_time, d.name", (start.isoformat(), end, *statuses))
    return [dict(r) for r in rows]


@router.post("/appointments/{ref}/cancel")
def cancel_appointment(ref: str, body: CancelIn, conn=Depends(get_conn)) -> dict:
    try:
        appt = booking.cancel(conn, ref, f"Cancelled by staff: {body.reason.strip()}")
    except BookingError as exc:
        raise HTTPException(404 if exc.code == "NOT_FOUND" else 409, str(exc))
    return {"ref": appt["ref"], "status": appt["status"]}


# ------------------------------------------------------------------ follow-ups

@router.get("/followups")
def followups(status: str = "OPEN", conn=Depends(get_conn)) -> list[dict]:
    where = "" if status == "ALL" else "WHERE f.status = ?"
    params = () if status == "ALL" else (status,)
    rows = conn.execute(
        f"SELECT f.*, p.name AS patient, p.demo_contact, c.language, c.summary AS call_summary"
        f" FROM followups f LEFT JOIN patients p ON p.id = f.patient_id LEFT JOIN calls c ON c.id = f.call_id"
        f" {where} ORDER BY f.status = 'RESOLVED', f.priority = 'NORMAL', f.created_at", params)
    return [dict(r) for r in rows]


@router.post("/followups/{followup_id}/resolve")
def resolve_followup(followup_id: int, body: ResolveIn, conn=Depends(get_conn)) -> dict:
    with transaction(conn):
        row = conn.execute("SELECT status FROM followups WHERE id = ?", (followup_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Follow-up not found")
        if row["status"] == "RESOLVED":
            raise HTTPException(409, "Already resolved")
        conn.execute("UPDATE followups SET status = 'RESOLVED', resolution_note = ?, resolved_at = ? WHERE id = ?",
                     (body.note.strip(), _iso(now_ist()), followup_id))
    return {"id": followup_id, "status": "RESOLVED"}


# ------------------------------------------------------------------ calls

@router.get("/calls")
def calls(limit: int = 50, conn=Depends(get_conn)) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, p.name AS patient, p.demo_contact,"
        " (SELECT COUNT(*) FROM turns t WHERE t.call_id = c.id) AS turn_count"
        " FROM calls c LEFT JOIN patients p ON p.id = c.patient_id"
        " ORDER BY c.started_at DESC LIMIT ?", (max(1, min(limit, 200)),))
    return [dict(r) for r in rows]


@router.get("/calls/{call_id}")
def call_detail(call_id: int, conn=Depends(get_conn)) -> dict:
    call = conn.execute("SELECT c.*, p.name AS patient, p.demo_contact FROM calls c"
                        " LEFT JOIN patients p ON p.id = c.patient_id WHERE c.id = ?", (call_id,)).fetchone()
    if call is None:
        raise HTTPException(404, "Call not found")
    turns = conn.execute("SELECT role, text, lang, gnani_request_id, stt_ms, llm_ms, tts_ms, created_at"
                         " FROM turns WHERE call_id = ? ORDER BY id", (call_id,))
    return {"call": dict(call), "turns": [dict(t) for t in turns]}


# ------------------------------------------------------------------ doctors & leave

@router.get("/doctors")
def doctors(conn=Depends(get_conn)) -> list[dict]:
    today = _today()
    out = []
    for d in conn.execute("SELECT * FROM doctors ORDER BY id"):
        weekly = json.loads(d["weekly_json"])
        leave = [r["date"] for r in conn.execute(
            "SELECT date FROM doctor_leave WHERE doctor_id = ? AND date >= ? ORDER BY date", (d["id"], today))]
        out.append({"id": d["id"], "name": d["name"], "specialty": d["specialty"], "fee_inr": d["fee_inr"],
                    "slot_mins": d["slot_mins"], "days": list(weekly),
                    "hours": [f"{a}–{b}" for a, b in next(iter(weekly.values()), [])],
                    "leave": leave, "active": bool(d["active"])})
    return out


@router.post("/doctors/{doctor_id}/leave")
def set_leave(doctor_id: str, body: LeaveIn, conn=Depends(get_conn)) -> dict:
    if body.date < now_ist().date():
        raise HTTPException(422, "Leave can only be set for today or later")
    try:
        booking.set_leave(conn, doctor_id, body.date.isoformat(), body.on)
    except booking.NotFound as exc:
        raise HTTPException(404, str(exc))
    clashes = conn.execute("SELECT COUNT(*) FROM appointments WHERE doctor_id = ? AND date = ? AND status = 'CONFIRMED'",
                           (doctor_id, body.date.isoformat())).fetchone()[0] if body.on else 0
    return {"doctor_id": doctor_id, "date": body.date.isoformat(), "on_leave": body.on,
            "existing_bookings_that_day": clashes}


# ------------------------------------------------------------------ FAQs

@router.get("/faqs")
def faqs(conn=Depends(get_conn)) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM faqs ORDER BY category, id")]


@router.put("/faqs/{faq_id}")
def update_faq(faq_id: str, body: FaqIn, conn=Depends(get_conn)) -> dict:
    cur = conn.execute("UPDATE faqs SET answer_en = ?, answer_hi = ?, active = ? WHERE id = ?",
                       (body.answer_en.strip(), body.answer_hi.strip(), int(body.active), faq_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "FAQ not found")
    return {"id": faq_id, "saved": True}


# ------------------------------------------------------------------ demo reset

@router.post("/reset")
def reset(request: Request, conn=Depends(get_conn), eng=Depends(engine)) -> dict:
    """Reseed all synthetic data (dates relative to now) and drop live call sessions."""
    eng.sessions.clear()
    counts = seed.seed(conn)
    return {"ok": True, "counts": counts}
