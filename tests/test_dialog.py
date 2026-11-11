"""Dialog engine with a scripted fake LLM: proves the code-level guarantees."""
import asyncio

import pytest
from google.genai import types

from app import booking
from app.dialog import DialogEngine
from app.llm import LLMError

from .conftest import MONDAY_8AM

TUE = "2026-11-17"


class FakeLLM:
    """Each step returns the next scripted list of (tool, args) calls."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0
        self.last_contents = None

    async def step(self, system, contents, tools):
        self.calls += 1
        self.last_contents = list(contents)
        if not self.steps:
            raise AssertionError("LLM called more times than scripted")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return types.Content(role="model", parts=[types.Part.from_function_call(name=n, args=a) for n, a in step])


def say(text="ok", lang="hi-en"):
    return ("say", {"text": text, "language": lang})


def run(coro):
    return asyncio.run(coro)


def engine(*steps):
    return DialogEngine(FakeLLM(*steps), now_fn=lambda: MONDAY_8AM)


def ai_bookings(conn):
    return conn.execute("SELECT * FROM appointments WHERE source = 'AI'").fetchall()


PROPOSE = ("propose_booking", {"doctor": "Chandrakar", "date": TUE, "time": "10:30"})


# ------------------------------------------------------------------ confirmation gate

def test_booking_needs_proposal_then_yes_on_a_later_turn(conn):
    eng = engine([("find_slots", {"doctor": "Dr. Chandrakar", "date": TUE, "time": "10:30"})], [say("2 slots")],
                 [PROPOSE], [say("confirm?")],
                 [("confirm_pending", {})], [say("booked")])
    sess, _ = eng.start(conn, "PAT-1020")
    r1 = run(eng.turn(conn, sess.id, "Kal Dr. Chandrakar ka 10:30 ka slot?"))
    assert r1.card["type"] == "slots" and r1.card["slots"][0]["time"] == "10:30"
    r2 = run(eng.turn(conn, sess.id, "10:30 wala"))
    assert r2.card["type"] == "pending" and r2.card["fee_inr"] == 400 and not ai_bookings(conn)
    r3 = run(eng.turn(conn, sess.id, "Haan, book kar do"))
    rows = ai_bookings(conn)
    assert r3.card["type"] == "confirmed" and len(rows) == 1
    assert (rows[0]["doctor_id"], rows[0]["date"], rows[0]["start_time"], rows[0]["patient_id"]) == \
        ("DOC-101", TUE, "10:30", "PAT-1020")
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (sess.call_id,)).fetchone()
    assert call["outcome"] == "BOOKED" and call["summary"].startswith(f"Booked {rows[0]['ref']}")


def test_confirm_in_same_turn_as_proposal_is_refused(conn):
    fake_steps = ([PROPOSE, ("confirm_pending", {})], [say()])
    eng = engine(*fake_steps)
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Book Chandrakar tomorrow 10:30, yes confirm"))
    assert not ai_bookings(conn)
    # The model was told why.
    response = eng.llm.last_contents[-1].parts[1].function_response.response
    assert response["error"] == "NOT_CONFIRMED_YET"


def test_confirm_without_a_yes_is_refused(conn):
    eng = engine([PROPOSE], [say()], [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    run(eng.turn(conn, sess.id, "Aur fee kitni hai?"))
    assert not ai_bookings(conn)
    assert eng.llm.last_contents[-1].parts[0].function_response.response["error"] == "CALLER_HAS_NOT_SAID_YES"


def test_no_clears_pending(conn):
    eng = engine([PROPOSE], [say()], [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    run(eng.turn(conn, sess.id, "Nahi, rehne do"))
    assert sess.pending is None and not ai_bookings(conn)


def test_confirm_button_counts_as_yes(conn):
    eng = engine([PROPOSE], [say()], [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    run(eng.turn(conn, sess.id, "Confirm", confirm_click=True))
    assert len(ai_bookings(conn)) == 1


def test_slot_taken_between_proposal_and_yes(conn):
    eng = engine([PROPOSE], [say()], [("confirm_pending", {})], [say("taken, try 10:15")])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    booking.book(conn, "DOC-101", "PAT-1018", TUE, "10:30", source="STAFF", now=MONDAY_8AM)  # front desk wins
    run(eng.turn(conn, sess.id, "haan"))
    result = eng.llm.last_contents[-1].parts[0].function_response.response
    assert result["error"] == "SLOT_UNAVAILABLE" and result["alternatives"]
    assert not ai_bookings(conn)


def test_new_caller_must_give_name_and_gets_synthetic_record(conn):
    eng = engine([PROPOSE], [say("name?")],
                 [("propose_booking", {**PROPOSE[1], "patient_name": "Asha Rao"})], [say()],
                 [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, None)
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    assert sess.pending is None
    run(eng.turn(conn, sess.id, "Mera naam Asha Rao hai"))
    run(eng.turn(conn, sess.id, "yes"))
    row = ai_bookings(conn)[0]
    patient = conn.execute("SELECT * FROM patients WHERE id = ?", (row["patient_id"],)).fetchone()
    assert patient["name"] == "Asha Rao" and patient["demo_contact"].startswith("DEMO-IN-")


def test_propose_rejects_closed_or_booked_slot(conn):
    eng = engine([("propose_booking", {"doctor": "Deshmukh", "date": "2026-11-18", "time": "10:00"})], [say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Deshmukh Wednesday 10"))
    assert sess.pending is None
    assert eng.llm.last_contents[-1].parts[0].function_response.response["error"] == "SLOT_UNAVAILABLE"


# ------------------------------------------------------------------ cancel / reschedule

def _seeded_ref(conn, patient):
    return conn.execute("SELECT ref FROM appointments WHERE patient_id = ? AND status = 'CONFIRMED'",
                        (patient,)).fetchone()["ref"]


def test_cancel_own_appointment(conn):
    ref = _seeded_ref(conn, "PAT-1004")
    eng = engine([("my_appointments", {})], [("propose_cancel", {"ref": ref})], [say()],
                 [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, "PAT-1004")
    run(eng.turn(conn, sess.id, "Mera appointment cancel karna hai"))
    run(eng.turn(conn, sess.id, "haan"))
    assert booking.get_appointment(conn, ref)["status"] == "CANCELLED"


def test_cannot_touch_another_patients_booking(conn):
    other = _seeded_ref(conn, "PAT-1001")
    eng = engine([("propose_cancel", {"ref": other})], [say()])
    sess, _ = eng.start(conn, "PAT-1004")
    run(eng.turn(conn, sess.id, f"cancel {other}"))
    assert sess.pending is None and booking.get_appointment(conn, other)["status"] == "CONFIRMED"


def test_reschedule(conn):
    ref = _seeded_ref(conn, "PAT-1001")
    eng = engine([("propose_reschedule", {"ref": ref, "date": "2026-11-19", "time": "17:00"})], [say()],
                 [("confirm_pending", {})], [say()])
    sess, _ = eng.start(conn, "PAT-1001")
    run(eng.turn(conn, sess.id, "Thursday evening 5 baje kar do"))
    r = run(eng.turn(conn, sess.id, "ji haan"))
    assert r.card["type"] == "confirmed" and booking.get_appointment(conn, ref)["status"] == "CANCELLED"
    assert booking.get_appointment(conn, r.card["new_ref"])["start_time"] == "17:00"


# ------------------------------------------------------------------ safety & failure paths

def test_emergency_never_reaches_llm(conn):
    eng = engine()           # any LLM call would raise
    sess, _ = eng.start(conn, "PAT-1020")
    r = run(eng.turn(conn, sess.id, "Mere seene mein dard ho raha hai, kya karu?"))
    assert r.source == "safety" and r.card == {"type": "escalated", "priority": "URGENT", "emergency": True}
    assert "108" in r.reply and eng.llm.calls == 0
    fup = conn.execute("SELECT * FROM followups WHERE call_id = ?", (sess.call_id,)).fetchone()
    assert fup["priority"] == "URGENT" and fup["reason"] == "MEDICAL" and fup["status"] == "OPEN"


def test_human_request_creates_normal_ticket(conn):
    eng = engine()
    sess, _ = eng.start(conn, "PAT-1020")
    r = run(eng.turn(conn, sess.id, "I want to talk to a human"))
    assert r.lang == "en-IN" and r.card["priority"] == "NORMAL" and eng.llm.calls == 0


def test_two_silent_turns_escalate(conn):
    eng = engine()
    sess, _ = eng.start(conn, "PAT-1020")
    assert run(eng.turn(conn, sess.id, "")).source == "repeat"
    assert run(eng.turn(conn, sess.id, "  ")).escalated
    assert conn.execute("SELECT reason FROM followups WHERE call_id = ?", (sess.call_id,)).fetchone()[0] == "NOT_UNDERSTOOD"


def test_llm_failure_gives_graceful_reply_and_clean_history(conn):
    eng = engine(LLMError("quota"))
    sess, _ = eng.start(conn, "PAT-1020")
    r = run(eng.turn(conn, sess.id, "Clinic kab khulta hai?"))
    assert r.source == "error" and r.escalated
    roles = [c.role for c in sess.contents]
    assert roles == ["model", "user", "model"]          # greeting, question, fallback reply; no dangling tool call


def test_phone_numbers_are_not_stored(conn):
    eng = engine([say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Mera number 98765 43210 hai"))
    texts = [r[0] for r in conn.execute("SELECT text FROM turns WHERE call_id = ?", (sess.call_id,))]
    assert not any("98765" in t for t in texts)


def test_say_together_with_a_tool_is_deferred(conn):
    eng = engine([("find_slots", {"doctor": "Chandrakar"}), say("premature")], [say("after reading slots")])
    sess, _ = eng.start(conn, "PAT-1020")
    r = run(eng.turn(conn, sess.id, "Chandrakar slots?"))
    assert r.reply == "after reading slots"


def test_unknown_doctor_lists_real_doctors(conn):
    eng = engine([("find_slots", {"doctor": "Dr. Sharma"})], [say()])
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Dr. Sharma ka appointment"))
    res = eng.llm.last_contents[-1].parts[0].function_response.response
    assert res["error"] == "DOCTOR_NOT_FOUND" and len(res["doctors_at_this_clinic"]) == 4


@pytest.mark.parametrize("raw,clean", [
    ("आप parso یعنی 11 November को आ सकते हैं।", "आप parso 11 November को आ सकते हैं।"),
    ("जी, kal kal 10 November ko", "जी, kal 10 November ko"),
    ("Dr. Baghel is free at 10:00.", "Dr. Baghel is free at 10:00."),
])
def test_clean_reply(raw, clean):
    from app.dialog import clean_reply
    assert clean_reply(raw) == clean


def test_unknown_patient_rejected(conn):
    with pytest.raises(booking.NotFound):
        engine().start(conn, "PAT-9999")


# ------------------------------------------------------------------ reply language

def test_an_english_reply_to_a_hinglish_caller_is_asked_for_again(conn):
    eng = engine([say("Sure, the clinic opens at nine in the morning.", "en-IN")],
                 [say("Ji, clinic subah 9 baje khulta hai.", "hi-en")])
    sess, _ = eng.start(conn, "PAT-1020", "hi-en")
    r = run(eng.turn(conn, sess.id, "Clinic kab khulta hai?"))
    assert r.lang == "hi-en" and r.reply.startswith("Ji, clinic") and eng.llm.calls == 2


def test_the_language_retry_happens_only_once_and_never_for_english_callers(conn):
    eng = engine([say("Sure, the clinic opens at nine in the morning.", "en-IN")],
                 [say("Yes, it opens at nine in the morning.", "en-IN")],
                 [say("The clinic opens at nine in the morning.", "en-IN")])
    sess, _ = eng.start(conn, "PAT-1020", "hi-en")
    stubborn = run(eng.turn(conn, sess.id, "Clinic kab khulta hai?"))
    assert stubborn.lang == "en-IN" and eng.llm.calls == 2          # asked once, then accepted
    english = run(eng.turn(conn, sess.id, "What time does the clinic open on Saturday please?"))
    assert english.lang == "en-IN" and eng.llm.calls == 3
