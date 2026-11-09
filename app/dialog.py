"""Conversation engine: one turn = safety check -> Gemini tool loop -> spoken reply.

Guarantees enforced in code, not left to the prompt:
1. Red-flag symptoms, medicine/dosage questions and requests for a person never
   reach the LLM (safety.check runs first).
2. Nothing is written to the database in the same turn it is proposed.
   `propose_*` tools only store a pending action; `confirm_pending` executes it
   only on a later turn, and only if the caller's latest words were a clear yes
   (or they pressed Confirm).
3. Facts (fees, timings, doctors, FAQs) come from the database on every turn,
   so admin edits take effect immediately.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from google.genai import types

from . import booking, safety
from .booking import BookingError, _iso
from .config import now_ist
from .db import transaction
from .llm import LLM, LLMError

MAX_STEPS = 6
PENDING_TTL_TURNS = 3
LANGS = ("hi-IN", "en-IN", "hi-en")
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ============================================================== session state

@dataclass
class Pending:
    kind: str                 # book | cancel | reschedule
    data: dict
    turn: int


@dataclass
class Session:
    id: str
    call_id: int
    patient_id: str | None
    patient_name: str | None
    lang: str = "hi-en"
    contents: list = field(default_factory=list)
    pending: Pending | None = None
    turn: int = 0
    misses: int = 0
    intent: str | None = None
    outcome: str | None = None
    escalated: bool = False
    last_bot_text: str = ""


@dataclass
class TurnResult:
    reply: str
    lang: str
    card: dict | None = None
    escalated: bool = False
    tools: list[str] = field(default_factory=list)
    llm_ms: int = 0
    source: str = "llm"       # llm | safety | repeat | error


class SessionNotFound(KeyError):
    pass


# ============================================================== yes / no detection

def _words(text: str) -> str:
    t = unicodedata.normalize("NFC", text.lower())
    return " " + re.sub(r"[^\w\sऀ-ॿ]", " ", t) + " "

_YES = re.compile(
    r"\s(haan|haa|han|hanji|haanji|ha|yes|yeah|yep|ok|okay|okk|confirm|confirmed|sure|done|chalega|"
    r"bilkul|theek hai|thik hai|thick hai|kar do|kardo|kar dijiye|kar dijie|kar dena|book kar|go ahead|"
    r"please do|perfect|हाँ|हां|हा|हान|ओके|ठीक है|कर दो|कर दीजिए|कर दीजिये|कर दें|बुक कर|कन्फर्म|बिल्कुल|चलेगा)\s")
_NO = re.compile(r"\s(no|nope|nahi|nahin|nhi|mat|don t|dont|wait|ruko|ruk jao|stop|not now|"
                 r"नहीं|नही|मत|रुको|रुकिए)\s")


def is_yes(text: str) -> bool:
    w = _words(text)
    if _NO.search(w):
        return False
    if _YES.search(w):
        return True
    return len(w.split()) <= 2 and bool(re.search(r"\s(ji|jee|जी)\s", w))


def is_no(text: str) -> bool:
    w = _words(text)
    return bool(_NO.search(w)) and not _YES.search(w)


# ============================================================== tool declarations

def _decl(name: str, description: str, props: dict | None = None, required: list[str] | None = None):
    schema = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return types.FunctionDeclaration(name=name, description=description, parameters_json_schema=schema)

_DATE = {"type": "string", "description": "YYYY-MM-DD, resolved from the calendar in the instructions"}
_TIME = {"type": "string", "description": "HH:MM 24-hour, e.g. 17:15"}
_DOCTOR = {"type": "string", "description": "Doctor name or specialty as the caller said it"}

TOOLS = [
    _decl("find_slots", "Find the nearest open appointment slots (max 2) for a doctor or specialty.",
          {"doctor": _DOCTOR, "date": _DATE, "time": _TIME,
           "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"],
                           "description": "subah=morning, dopahar=afternoon, shaam=evening"}}, ["doctor"]),
    _decl("propose_booking", "Hold a specific slot and get read-back details. Does NOT book. "
          "Call only after the caller chose a slot and you know their name.",
          {"doctor": _DOCTOR, "date": _DATE, "time": _TIME,
           "patient_name": {"type": "string", "description": "Required only for a new caller"}},
          ["doctor", "date", "time"]),
    _decl("my_appointments", "List the caller's upcoming appointments with their booking references."),
    _decl("propose_cancel", "Prepare cancelling one appointment. Does NOT cancel.",
          {"ref": {"type": "string", "description": "Booking reference like CC-1042"}}, ["ref"]),
    _decl("propose_reschedule", "Prepare moving an appointment to a new open slot. Does NOT change anything. "
          "Use find_slots first to pick the new slot.",
          {"ref": {"type": "string"}, "date": _DATE, "time": _TIME,
           "doctor": {"type": "string", "description": "Only if changing doctor"}}, ["ref", "date", "time"]),
    _decl("confirm_pending", "Execute the booking/cancellation/reschedule proposed earlier. "
          "Call ONLY when the caller has just clearly said yes to the details you read back."),
    _decl("clear_pending", "Drop the proposed action because the caller declined or changed their mind."),
    _decl("escalate", "Create a callback ticket for clinic staff (caller wants a person, a request you cannot "
          "handle, billing disputes, or any medical question).",
          {"reason": {"type": "string", "enum": ["MEDICAL", "HUMAN_REQUEST", "OTHER"]},
           "summary": {"type": "string", "description": "One line for staff, in English"}},
          ["reason", "summary"]),
    _decl("say", "Speak the reply to the caller. Every turn must end with exactly one say call.",
          {"text": {"type": "string", "description": "1-2 short sentences"},
           "language": {"type": "string", "enum": list(LANGS)}}, ["text", "language"]),
]


# ============================================================== engine

class DialogEngine:
    def __init__(self, llm: LLM | None, now_fn=now_ist):
        self.llm = llm
        self.now = now_fn
        self.sessions: dict[str, Session] = {}

    # ---------------------------------------------------------------- lifecycle

    def start(self, conn, patient_id: str | None = None, lang: str | None = None) -> tuple[Session, str]:
        name = None
        if patient_id:
            row = conn.execute("SELECT name, preferred_lang FROM patients WHERE id = ?", (patient_id,)).fetchone()
            if row is None:
                raise booking.NotFound(f"No patient {patient_id}")
            name, lang = row["name"], lang or row["preferred_lang"]
        lang = lang if lang in LANGS else "hi-en"
        sid = uuid.uuid4().hex
        with transaction(conn):
            call_id = conn.execute(
                "INSERT INTO calls (session_id, patient_id, started_at, language, intent, outcome)"
                " VALUES (?, ?, ?, ?, 'GENERAL', 'IN_PROGRESS')",
                (sid, patient_id, _iso(self.now()), lang)).lastrowid
        sess = Session(sid, call_id, patient_id, name, lang)
        self.sessions[sid] = sess
        greeting = safety.reply("GREETING", lang)
        sess.last_bot_text = greeting
        self._log(conn, sess, "bot", greeting, lang)
        sess.contents.append(types.Content(role="model", parts=[types.Part.from_text(text=greeting)]))
        return sess, greeting

    def get(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise SessionNotFound(session_id) from None

    def end(self, conn, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess:
            conn.execute("UPDATE calls SET ended_at = ?, outcome = ? WHERE id = ?",
                         (_iso(self.now()), sess.outcome or "ANSWERED", sess.call_id))

    # ---------------------------------------------------------------- one turn

    async def turn(self, conn, session_id: str, text: str, *, confirm_click: bool = False,
                   stt: dict | None = None) -> TurnResult:
        sess = self.get(session_id)
        sess.turn += 1
        text = (text or "").strip()
        if text:
            sess.lang = safety.guess_lang(text, fallback=sess.lang)
        self._log(conn, sess, "user", text or "(no speech detected)", sess.lang, stt=stt)

        if not text:
            sess.misses += 1
            if sess.misses >= 2:
                return self._escalate_fixed(conn, sess, "NOT_UNDERSTOOD", "NOT_UNDERSTOOD", "NORMAL",
                                            "Caller could not be understood twice", stt)
            return self._fixed_reply(conn, sess, safety.reply("REPEAT", sess.lang), "repeat")

        verdict = safety.check(text)
        if verdict.kind:
            reason, prio, kind = {"EMERGENCY": ("MEDICAL", "URGENT", "EMERGENCY"),
                                  "MEDICAL": ("MEDICAL", "URGENT", "MEDICAL"),
                                  "HUMAN": ("HUMAN_REQUEST", "NORMAL", "HUMAN")}[verdict.kind]
            sess.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
            return self._escalate_fixed(conn, sess, kind, reason, prio, text, stt,
                                        emergency=verdict.kind == "EMERGENCY")

        affirmative = confirm_click or is_yes(text)
        if sess.pending and (is_no(text) or sess.turn - sess.pending.turn > PENDING_TTL_TURNS):
            sess.pending = None

        if self.llm is None:
            return self._system_error(conn, sess, "LLM not configured")

        mark = len(sess.contents)
        user_text = f"{text}\n[caller language: {sess.lang}]" + (
            "\n[Caller pressed the Confirm button]" if confirm_click else "")
        sess.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
        ctx = _TurnCtx(affirmative=affirmative)
        t0 = time.perf_counter()
        try:
            final = await self._loop(conn, sess, ctx)
        except LLMError as exc:
            del sess.contents[mark:]
            sess.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
            return self._system_error(conn, sess, str(exc))
        llm_ms = round((time.perf_counter() - t0) * 1000)

        reply_text, reply_lang = final
        sess.misses = 0
        sess.lang = reply_lang
        sess.last_bot_text = reply_text
        self._log(conn, sess, "bot", reply_text, reply_lang, llm_ms=llm_ms)
        self._update_call(conn, sess)
        return TurnResult(reply_text, reply_lang, ctx.card, ctx.escalated, ctx.tools, llm_ms)

    async def _loop(self, conn, sess: Session, ctx: "_TurnCtx") -> tuple[str, str]:
        for _ in range(MAX_STEPS):
            content = await self.llm.step(self._system_prompt(conn, sess), sess.contents, TOOLS)
            sess.contents.append(content)
            calls = [p.function_call for p in content.parts if p.function_call]
            if not calls:
                text = " ".join(p.text for p in content.parts if p.text).strip()
                if text:
                    return text, sess.lang
                raise LLMError("Model returned neither a tool call nor text")
            responses, final = [], None
            others = [c for c in calls if c.name != "say"]
            for call in calls:
                args = dict(call.args or {})
                if call.name == "say":
                    if others:
                        result = {"status": "IGNORED", "message": "Read the tool results first, then call say again."}
                    else:
                        said = clean_reply(args.get("text") or "")
                        declared = args.get("language") if args.get("language") in LANGS else sess.lang
                        # The voice must match the script actually written, whatever the model declared.
                        final = (said, safety.guess_lang(said, fallback=declared))
                        result = {"status": "SPOKEN"}
                else:
                    ctx.tools.append(call.name)
                    result = self._run_tool(conn, sess, ctx, call.name, args)
                responses.append(types.Part.from_function_response(name=call.name, response=result))
            sess.contents.append(types.Content(role="user", parts=responses))
            if final and final[0]:
                return final
        raise LLMError("Too many tool steps without a reply")

    # ---------------------------------------------------------------- tools

    def _run_tool(self, conn, sess: Session, ctx: "_TurnCtx", name: str, args: dict) -> dict:
        try:
            handler = getattr(self, f"_tool_{name}")
        except AttributeError:
            return {"error": "UNKNOWN_TOOL"}
        try:
            return handler(conn, sess, ctx, **{k: v for k, v in args.items() if v not in (None, "")})
        except TypeError as exc:
            return {"error": "BAD_ARGUMENTS", "message": str(exc)}
        except BookingError as exc:
            return {"error": exc.code, "message": str(exc)}

    def _resolve_doctor(self, conn, doctor: str):
        doc = booking.find_doctor(conn, doctor)
        if doc is None:
            names = [f"{r['name']} ({r['specialty']})" for r in conn.execute("SELECT * FROM doctors WHERE active = 1")]
            return None, {"error": "DOCTOR_NOT_FOUND", "message": f"No doctor matches '{doctor}'.",
                          "doctors_at_this_clinic": names}
        return doc, None

    def _slot_view(self, slot: booking.Slot) -> dict:
        d = date.fromisoformat(slot.date)
        return {"date": slot.date, "weekday": EN_DAYS[d.weekday()], "time": slot.start}

    def _clamp_day(self, day: str | None) -> str | None:
        if not day:
            return None
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return None
        return max(d, self.now().date()).isoformat()

    def _tool_find_slots(self, conn, sess, ctx, doctor, date=None, time=None, part_of_day=None):
        sess.intent = sess.intent if sess.intent in ("RESCHEDULE",) else "BOOK"
        doc, err = self._resolve_doctor(conn, doctor)
        if err:
            return err
        day = self._clamp_day(date)
        if time and not re.fullmatch(r"\d{1,2}:\d{2}", time):
            time = None
        slots = booking.find_slots(conn, doc["id"], day, preferred_time=time and time.zfill(5),
                                   part_of_day=part_of_day, now=self.now())
        result = {"doctor": doc["name"], "specialty": doc["specialty"], "fee_inr": doc["fee_inr"],
                  "slots": [self._slot_view(s) for s in slots]}
        if day:
            if booking.on_leave(conn, doc["id"], day):
                result["note"] = f"{doc['name']} is on leave on {day}."
            elif slots and slots[0].date != day:
                result["note"] = f"No open slots on {day}; these are the nearest."
        if not slots:
            result["note"] = "No open slots in the next 7 days."
        ctx.card = {"type": "slots", "doctor": doc["name"], "fee_inr": doc["fee_inr"], "slots": result["slots"]}
        return result

    def _require_open(self, conn, doc, day: str, start: str) -> dict | None:
        open_starts = {s.start for s in booking.available_slots(conn, doc["id"], day, self.now())}
        if start not in open_starts:
            alts = booking.find_slots(conn, doc["id"], day, preferred_time=start, now=self.now())
            return {"error": "SLOT_UNAVAILABLE", "message": f"{doc['name']} is not free on {day} at {start}.",
                    "alternatives": [self._slot_view(s) for s in alts]}
        return None

    def _tool_propose_booking(self, conn, sess, ctx, doctor, date, time, patient_name=None):
        sess.intent = "BOOK"
        doc, err = self._resolve_doctor(conn, doctor)
        if err:
            return err
        start = time.zfill(5)
        err = self._require_open(conn, doc, date, start)
        if err:
            return err
        if patient_name:
            sess.patient_name = patient_name.strip()
        if not sess.patient_id and not sess.patient_name:
            return {"error": "NEED_NAME", "message": "Ask the caller for their full name first."}
        details = {"doctor": doc["name"], "doctor_id": doc["id"], "date": date, "time": start,
                   "weekday": EN_DAYS[_d(date).weekday()], "fee_inr": doc["fee_inr"],
                   "patient": sess.patient_name}
        sess.pending = Pending("book", details, sess.turn)
        ctx.card = {"type": "pending", "action": "book", **_public(details)}
        return {"status": "AWAITING_CONFIRMATION", "read_back": _public(details),
                "instruction": "Read these details back and ask the caller to say yes to confirm."}

    def _tool_my_appointments(self, conn, sess, ctx):
        if not sess.patient_id:
            return {"error": "NEW_CALLER", "message": "No bookings on file for a new caller; ask for the booking reference."}
        rows = booking.upcoming_for_patient(conn, sess.patient_id, self.now())
        return {"appointments": [{"ref": r["ref"], "doctor": r["doctor_name"], "date": r["date"],
                                  "weekday": EN_DAYS[_d(r["date"]).weekday()], "time": r["start_time"]}
                                 for r in rows]}

    def _own_appointment(self, conn, sess, ref: str):
        appt = booking.get_appointment(conn, ref)
        if sess.patient_id and appt["patient_id"] != sess.patient_id:
            raise booking.NotFound(f"{appt['ref']} is not one of this caller's appointments")
        if appt["status"] != "CONFIRMED":
            raise booking.InvalidState(f"{appt['ref']} is already {appt['status'].lower()}")
        return appt

    def _tool_propose_cancel(self, conn, sess, ctx, ref):
        sess.intent = "CANCEL"
        appt = self._own_appointment(conn, sess, ref)
        doc = booking.get_doctor(conn, appt["doctor_id"])
        details = {"ref": appt["ref"], "doctor": doc["name"], "date": appt["date"], "time": appt["start_time"],
                   "weekday": EN_DAYS[_d(appt["date"]).weekday()]}
        sess.pending = Pending("cancel", details, sess.turn)
        ctx.card = {"type": "pending", "action": "cancel", **details}
        return {"status": "AWAITING_CONFIRMATION", "read_back": details,
                "instruction": "Read back which appointment will be cancelled and ask for a yes."}

    def _tool_propose_reschedule(self, conn, sess, ctx, ref, date, time, doctor=None):
        sess.intent = "RESCHEDULE"
        appt = self._own_appointment(conn, sess, ref)
        if doctor:
            doc, err = self._resolve_doctor(conn, doctor)
            if err:
                return err
        else:
            doc = booking.get_doctor(conn, appt["doctor_id"])
        start = time.zfill(5)
        err = self._require_open(conn, doc, date, start)
        if err:
            return err
        details = {"ref": appt["ref"], "doctor": doc["name"], "doctor_id": doc["id"],
                   "from_date": appt["date"], "from_time": appt["start_time"],
                   "date": date, "time": start, "weekday": EN_DAYS[_d(date).weekday()], "fee_inr": doc["fee_inr"]}
        sess.pending = Pending("reschedule", details, sess.turn)
        ctx.card = {"type": "pending", "action": "reschedule", **_public(details)}
        return {"status": "AWAITING_CONFIRMATION", "read_back": _public(details),
                "instruction": "Read back the old and new times and ask for a yes."}

    def _tool_confirm_pending(self, conn, sess, ctx):
        p = sess.pending
        if p is None:
            return {"error": "NOTHING_PENDING", "message": "There is nothing to confirm. Propose first."}
        if p.turn == sess.turn:
            return {"error": "NOT_CONFIRMED_YET",
                    "message": "You proposed this in this same turn. Read the details back and wait for the caller's yes."}
        if not ctx.affirmative:
            return {"error": "CALLER_HAS_NOT_SAID_YES",
                    "message": "The caller's last words were not a clear yes. Ask them to confirm."}
        d, now = p.data, self.now()
        try:
            if p.kind == "book":
                patient_id = sess.patient_id or booking.create_patient(conn, d["patient"], sess.lang)
                sess.patient_id = patient_id
                conn.execute("UPDATE calls SET patient_id = ? WHERE id = ?", (patient_id, sess.call_id))
                appt = booking.book(conn, d["doctor_id"], patient_id, d["date"], d["time"], "AI", now)
                sess.outcome = "BOOKED"
                summary = f"Booked {appt['ref']}: {d['doctor']} on {d['date']} at {d['time']}"
                ctx.card = {"type": "confirmed", "action": "book", "ref": appt["ref"], **_public(d)}
                result = {"status": "BOOKED", "ref": appt["ref"], **_public(d)}
            elif p.kind == "cancel":
                appt = booking.cancel(conn, d["ref"], "Cancelled by patient via CareCall AI", now)
                sess.outcome = "CANCELLED"
                summary = f"Cancelled {appt['ref']} ({d['doctor']} {d['date']} {d['time']})"
                ctx.card = {"type": "confirmed", "action": "cancel", **d}
                result = {"status": "CANCELLED", **d}
            else:
                appt = booking.reschedule(conn, d["ref"], d["date"], d["time"], d["doctor_id"], now)
                sess.outcome = "RESCHEDULED"
                summary = f"Moved {d['ref']} to {appt['ref']}: {d['doctor']} {d['date']} {d['time']}"
                ctx.card = {"type": "confirmed", "action": "reschedule", "new_ref": appt["ref"], **_public(d)}
                result = {"status": "RESCHEDULED", "new_ref": appt["ref"], **_public(d)}
        except BookingError as exc:
            sess.pending = None
            alts = []
            if p.kind in ("book", "reschedule"):
                alts = [self._slot_view(s) for s in booking.find_slots(
                    conn, d["doctor_id"], d["date"], preferred_time=d["time"], now=now)]
            return {"error": exc.code, "message": str(exc), "alternatives": alts}
        sess.pending = None
        conn.execute("UPDATE calls SET summary = ? WHERE id = ?", (summary, sess.call_id))
        return result

    def _tool_clear_pending(self, conn, sess, ctx):
        sess.pending = None
        return {"status": "CLEARED"}

    def _tool_escalate(self, conn, sess, ctx, reason, summary):
        reason = reason if reason in ("MEDICAL", "HUMAN_REQUEST", "OTHER") else "OTHER"
        priority = "URGENT" if reason == "MEDICAL" else "NORMAL"
        self._followup(conn, sess, reason, priority, summary)
        ctx.escalated = True
        ctx.card = {"type": "escalated", "priority": priority, "emergency": False}
        return {"status": "TICKET_CREATED", "priority": priority,
                "instruction": "Tell the caller staff will call them back."}

    # ---------------------------------------------------------------- fixed replies

    def _fixed_reply(self, conn, sess, text: str, source: str, card=None, escalated=False) -> TurnResult:
        sess.last_bot_text = text
        sess.contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
        self._log(conn, sess, "bot", text, sess.lang)
        self._update_call(conn, sess)
        return TurnResult(text, sess.lang, card, escalated, [], 0, source)

    def _escalate_fixed(self, conn, sess, kind, reason, priority, trigger, stt, emergency=False) -> TurnResult:
        sess.pending = None
        sess.intent = {"EMERGENCY": "MEDICAL", "MEDICAL": "MEDICAL", "HUMAN": "HUMAN"}.get(kind, "OTHER")
        self._followup(conn, sess, reason, priority, trigger)
        card = {"type": "escalated", "priority": priority, "emergency": emergency}
        return self._fixed_reply(conn, sess, safety.reply(kind, sess.lang), "safety", card, True)

    def _system_error(self, conn, sess, detail: str) -> TurnResult:
        self._followup(conn, sess, "SYSTEM_ERROR", "NORMAL", f"Assistant error: {detail[:200]}")
        card = {"type": "escalated", "priority": "NORMAL", "emergency": False}
        result = self._fixed_reply(conn, sess, safety.reply("SYSTEM_ERROR", sess.lang), "error", card, True)
        return result

    # ---------------------------------------------------------------- persistence

    def _followup(self, conn, sess, reason, priority, trigger) -> None:
        sess.escalated = True
        sess.outcome = "ESCALATED"
        conn.execute("INSERT INTO followups (call_id, patient_id, reason, priority, trigger_text, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (sess.call_id, sess.patient_id, reason, priority, safety.scrub(trigger)[:500],
                      _iso(self.now())))

    def _log(self, conn, sess, role, text, lang, stt: dict | None = None, llm_ms: int | None = None) -> None:
        stt = stt or {}
        conn.execute("INSERT INTO turns (call_id, role, text, lang, gnani_request_id, stt_ms, llm_ms, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (sess.call_id, role, safety.scrub(text), lang, stt.get("request_id"), stt.get("latency_ms"),
                      llm_ms, _iso(self.now())))

    def log_tts(self, conn, sess: Session, request_id: str, tts_ms: int) -> None:
        """Attach the Timbre request to the bot turn just logged."""
        conn.execute("UPDATE turns SET gnani_request_id = COALESCE(gnani_request_id, ?), tts_ms = ?"
                     " WHERE id = (SELECT MAX(id) FROM turns WHERE call_id = ? AND role = 'bot')",
                     (request_id, tts_ms, sess.call_id))

    def _update_call(self, conn, sess) -> None:
        conn.execute("UPDATE calls SET language = ?, intent = COALESCE(?, intent), outcome = ?, escalated = ?"
                     " WHERE id = ?", (sess.lang, sess.intent, sess.outcome or "IN_PROGRESS",
                                       int(sess.escalated), sess.call_id))

    # ---------------------------------------------------------------- prompt

    def _system_prompt(self, conn, sess: Session) -> str:
        now = self.now()
        clinic = conn.execute("SELECT * FROM clinic").fetchone()
        days = []
        for i in range(8):
            d = now.date() + timedelta(days=i)
            label = "today" if i == 0 else "tomorrow (kal)" if i == 1 else "day after tomorrow (parso)" if i == 2 else ""
            days.append(f"{d.isoformat()} {EN_DAYS[d.weekday()]}" + (f" = {label}" if label else ""))
        doctors = []
        for r in conn.execute("SELECT * FROM doctors"):
            weekly = json.loads(r["weekly_json"])
            hours = ", ".join("-".join(w) for w in next(iter(weekly.values()), []))
            off = [d for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") if d not in weekly]
            leave = [l["date"] for l in conn.execute(
                "SELECT date FROM doctor_leave WHERE doctor_id = ? AND date >= ? ORDER BY date",
                (r["id"], now.date().isoformat()))]
            status = "" if r["active"] else " NOT TAKING APPOINTMENTS"
            doctors.append(f"- {r['name']} ({r['name_hi']}): {r['specialty']}; fee ₹{r['fee_inr']}; "
                           f"hours {hours}; closed {', '.join(off)}"
                           + (f"; ON LEAVE {', '.join(leave)}" if leave else "") + status)
        services = [f"- {s['name_en']}: ₹{s['fee_inr']}; {s['hours_en']}; prep: {s['prep_en']}"
                    for s in conn.execute("SELECT * FROM services")]
        faqs = [f"- [{f['category']}] {f['answer_en']}" for f in conn.execute("SELECT * FROM faqs WHERE active = 1")]
        if sess.patient_id:
            caller = f"Known caller: {sess.patient_name} ({sess.patient_id}). Do not ask for their name."
        elif sess.patient_name:
            caller = f"New caller who said their name is {sess.patient_name}."
        else:
            caller = "New caller; name unknown. Ask for their name only when you are about to propose a booking."
        pending = "none"
        if sess.pending:
            pending = f"{sess.pending.kind}: {json.dumps(_public(sess.pending.data), ensure_ascii=False)}"

        return f"""You are the AI receptionist of {clinic['name_en']}. You speak on a phone-style voice call.
Your job is administrative only: clinic information, appointment booking, cancellation and rescheduling.

NOW: {now.strftime('%Y-%m-%d %H:%M')} ({EN_DAYS[now.weekday()]}), India time.
CALENDAR (use these exact dates):
{chr(10).join(days)}

CALLER: {caller}
PENDING ACTION AWAITING THE CALLER'S YES: {pending}

CLINIC FACTS (the only facts you may state):
Address: {clinic['address_en']}; landmark: {clinic['landmark']}
Hours: {clinic['hours_en']}
Payment: {clinic['payment_modes']}
Doctors:
{chr(10).join(doctors)}
Services:
{chr(10).join(services)}
Policies:
{chr(10).join(faqs)}

RULES
1. Every turn ends with exactly one `say` call. Keep it to 1-2 short sentences (under 200 characters). Never use lists or markdown.
2. Never invent doctors, fees, timings, slots or policies. If something is not in CLINIC FACTS or a tool result, say you will ask staff to call back and use `escalate` with reason OTHER.
3. Slot availability comes only from `find_slots`. Offer at most 2 slots at a time and let the caller choose; do not call propose_booking or propose_reschedule in the same turn as find_slots unless the caller already named an exact time that is in the results.
3a. If find_slots returns DOCTOR_NOT_FOUND, tell the caller that doctor is not at this clinic and name the doctors and specialties that are available. Do not escalate for this.
4. Booking: find_slots -> caller picks a slot -> (new caller: get their name) -> propose_booking -> read back doctor, day, date, time and fee and ask "shall I confirm?" -> only when the caller then says yes, call confirm_pending. Never say an appointment is booked, cancelled or moved unless confirm_pending returned that status in this turn.
5. Cancel / reschedule: use my_appointments (known caller) or ask for the booking reference, then propose_cancel / propose_reschedule, read back, wait for yes, then confirm_pending. If the caller says no or changes their mind, call clear_pending.
6. If a tool returns an error, explain briefly and offer the alternatives it returned.
7. You are not a doctor. Never give medical advice, diagnoses, medicine or dose information, or interpret reports; use `escalate` with reason MEDICAL. A caller may mention a symptom while asking for an appointment: just help them book with a suitable doctor, without commenting on the symptom.
8. Language: each caller message ends with a [caller language: ...] tag. Reply in THAT language and set `language` to it:
   - en-IN for English;
   - hi-IN for pure Hindi, written in Devanagari;
   - hi-en for Hinglish, written in MIXED script: Hindi words in Devanagari, English words in Latin letters, e.g. "जी हाँ, Doctor Chandrakar कल 10:30 बजे available हैं।"
   Transcripts of Hinglish speech arrive in Devanagari with some English words in Latin; treat those as hi-en.
9. Write times as HH:MM in 24-hour form, dates as "12 November", money as ₹400, booking references as CC-1042. Do not spell numbers out (a later step converts them for speech).
10. Use the caller's name at most once per call. Be warm, brief and clear; older callers may be on the line.
"""


@dataclass
class _TurnCtx:
    affirmative: bool
    card: dict | None = None
    escalated: bool = False
    tools: list[str] = field(default_factory=list)


_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿ]+")
_REPEATED_WORD = re.compile(r"(?<!\S)(\S+)(\s+\1)+(?!\S)", re.I)


def clean_reply(text: str) -> str:
    """Remove Urdu/Arabic-script words (the Hindi voices can't read them) and stutters like 'kal kal'."""
    text = _ARABIC_SCRIPT.sub("", text)
    text = _REPEATED_WORD.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _d(day: str) -> date:
    return date.fromisoformat(day)


def _public(details: dict) -> dict:
    """Details safe to show the model and the UI (no internal IDs)."""
    return {k: v for k, v in details.items() if k not in ("doctor_id",)}
