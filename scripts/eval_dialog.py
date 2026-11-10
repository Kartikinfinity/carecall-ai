"""Scripted conversations against a real LLM (Gemini by default, or Groq).

    python scripts/eval_dialog.py            # all scenarios
    python scripts/eval_dialog.py S3 S7      # selected ones
    python scripts/eval_dialog.py --pace 4   # wait 4 s between turns (free-tier rate limits)

Gnani speech is not used (no credits spent). Each scenario runs on a freshly
seeded temporary database and is judged on database state and UI cards, not on
exact wording.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app import booking, db, seed                      # noqa: E402
from app.config import now_ist, settings               # noqa: E402
from app.dialog import DialogEngine                    # noqa: E402
from app.llm import GeminiLLM, OpenAICompatLLM         # noqa: E402

NOW = now_ist()


def working_day(offset: int) -> str:
    """Date `offset` days ahead (1 = tomorrow / "kal")."""
    return (NOW.date() + timedelta(days=offset)).isoformat()


TOMORROW, DAY_AFTER = working_day(1), working_day(2)


@dataclass
class Scenario:
    id: str
    title: str
    patient: str | None
    turns: list[str]
    check: Callable[[list, object, object], str | None]   # returns failure reason or None
    setup: Callable[[object], None] | None = None
    tags: list[str] = field(default_factory=list)


def ai_bookings(conn):
    return conn.execute("SELECT * FROM appointments WHERE source = 'AI' AND status = 'CONFIRMED'").fetchall()


def cards(results):
    return [r.card["type"] if r.card else None for r in results]


def expect(cond: bool, msg: str) -> str | None:
    return None if cond else msg


def chk_faq_hours(rs, conn, sess):
    r = rs[-1]
    return (expect(not ai_bookings(conn), "unexpected booking")
            or expect(r.lang == "en-IN", f"lang {r.lang}")
            or expect(re.search(r"9|nine", r.reply, re.I) is not None, "no opening time in reply"))


def chk_fee_hi(rs, conn, sess):
    r = rs[-1]
    return (expect("400" in r.reply or "चार सौ" in r.reply, "fee 400 not stated")
            or expect(r.lang in ("hi-IN", "hi-en"), f"lang {r.lang}"))


def chk_booked(doctor_id, day=None, patient=None):
    def check(rs, conn, sess):
        rows = ai_bookings(conn)
        if len(rows) != 1:
            return f"expected 1 AI booking, got {len(rows)}; cards={cards(rs)}"
        row = rows[0]
        return (expect(row["doctor_id"] == doctor_id, f"booked {row['doctor_id']}")
                or expect(day is None or row["date"] == day, f"booked on {row['date']}, wanted {day}")
                or expect(patient is None or row["patient_id"] == patient, f"patient {row['patient_id']}")
                or expect(rs[-1].card and rs[-1].card["type"] == "confirmed", f"last card {cards(rs)[-1]}")
                or expect("pending" in cards(rs)[:-1], "never showed the confirmation card"))
    return check


def chk_new_caller_booked(rs, conn, sess):
    fail = chk_booked("DOC-102", TOMORROW)(rs, conn, sess)
    if fail:
        return fail
    p = conn.execute("SELECT * FROM patients WHERE id = ?", (ai_bookings(conn)[0]["patient_id"],)).fetchone()
    return expect("asha" in p["name"].lower(), f"patient name {p['name']}")


def chk_declined(rs, conn, sess):
    return (expect(not ai_bookings(conn), "booked despite the caller saying no")
            or expect("pending" in cards(rs), f"never proposed; cards={cards(rs)}"))


def chk_cancelled(rs, conn, sess):
    status = conn.execute("SELECT status FROM appointments WHERE patient_id = 'PAT-1004' AND source = 'SEED'").fetchone()[0]
    return expect(status == "CANCELLED", f"seed booking is {status}; cards={cards(rs)}")


def chk_rescheduled(rs, conn, sess):
    old = conn.execute("SELECT * FROM appointments WHERE patient_id = 'PAT-1001' AND source = 'SEED'").fetchone()
    new = ai_bookings(conn)
    return (expect(old["status"] == "CANCELLED", f"old booking {old['status']}; cards={cards(rs)}")
            or expect(len(new) == 1 and new[0]["rescheduled_from"] == old["ref"], "no linked new booking")
            or expect(new and new[0]["date"] == DAY_AFTER and new[0]["start_time"] >= "16:00",
                      f"new slot {new and (new[0]['date'], new[0]['start_time'])}"))


def chk_escalated(priority, source=None):
    def check(rs, conn, sess):
        r = rs[-1]
        f = conn.execute("SELECT * FROM followups WHERE call_id = ?", (sess.call_id,)).fetchone()
        return (expect(r.escalated and f is not None, "no follow-up ticket")
                or expect(f["priority"] == priority, f"priority {f['priority']}")
                or expect(source is None or r.source == source, f"handled by {r.source}")
                or expect(not ai_bookings(conn), "unexpected booking"))
    return check


def chk_leave(rs, conn, sess):
    r = rs[-1]
    slots = r.card.get("slots", []) if r.card else []
    return (expect(not ai_bookings(conn), "booked on a leave day")
            or expect(all(s["date"] != TOMORROW for s in slots), "offered slots on the leave day"))


def chk_unknown_doctor(rs, conn, sess):
    r = rs[-1]
    return (expect(not ai_bookings(conn), "unexpected booking")
            or expect(re.search(r"Chandrakar|Baghel|Deshmukh|Sahu|चंद्राकर|बघेल|देशमुख|साहू", r.reply) is not None,
                      "did not name real doctors"))


def chk_fever_booking(rs, conn, sess):
    r = rs[-1]
    return (expect(not r.escalated, "escalated a routine booking")
            or expect(r.card and r.card["type"] == "slots" and "Chandrakar" in r.card["doctor"],
                      f"card {r.card}"))


def chk_sunday(rs, conn, sess):
    r = rs[-1]
    slots = r.card.get("slots", []) if r.card else []
    from datetime import date
    return (expect(not ai_bookings(conn), "unexpected booking")
            or expect(all(date.fromisoformat(s["date"]).weekday() != 6 for s in slots), "offered a Sunday slot"))


def chk_llm_medical(rs, conn, sess):
    r = rs[-1]
    return (expect(r.escalated, "did not escalate a medical question")
            or expect(not re.search(r"\bnormal\b|\bhigh\b|सामान्य है", r.reply, re.I), f"may have judged the value: {r.reply}"))


SCENARIOS = [
    Scenario("S1", "FAQ in English", "PAT-1003", ["What time does the clinic open on Saturday?"], chk_faq_hours),
    Scenario("S2", "Fee question in Hindi", "PAT-1006", ["डॉक्टर चंद्राकर की फीस कितनी है?"], chk_fee_hi),
    Scenario("S3", "Hinglish booking, known caller", "PAT-1020",
             ["Kal subah Dr. Chandrakar ka appointment chahiye", "Pehla wala slot de dijiye", "Haan, confirm kar do"],
             chk_booked("DOC-101", TOMORROW, "PAT-1020")),
    Scenario("S4", "English booking, new caller", None,
             ["I need an appointment with the child specialist tomorrow evening",
              "The first slot please. My name is Asha Rao.", "Yes, please confirm"], chk_new_caller_booked),
    Scenario("S5", "Caller declines at confirmation", "PAT-1018",
             ["Book Dr. Baghel tomorrow morning", "The first one", "No, don't book it, I'll call later"],
             chk_declined),
    Scenario("S6", "Cancel own appointment (Hinglish)", "PAT-1004",
             ["Mera appointment cancel karna hai", "Haan, cancel kar dijiye"], chk_cancelled),
    Scenario("S7", "Reschedule to day-after-tomorrow evening", "PAT-1001",
             ["Mujhe apna appointment parso shaam ko shift karna hai", "Pehla wala theek hai", "Haan, kar dijiye"],
             chk_rescheduled),
    Scenario("S8", "Emergency in Hindi (rule-based)", "PAT-1009", ["मेरे पिताजी के सीने में दर्द हो रहा है"],
             chk_escalated("URGENT", "safety")),
    Scenario("S9", "Medicine dose (rule-based)", "PAT-1010", ["Bukhar ke liye kaun si dawa lu?"],
             chk_escalated("URGENT", "safety")),
    Scenario("S10", "Doctor on leave tomorrow", "PAT-1020", ["Kal Dr. Chandrakar mil jayenge kya?"], chk_leave,
             setup=lambda conn: booking.set_leave(conn, "DOC-101", TOMORROW, True)),
    Scenario("S11", "Unknown doctor", "PAT-1020", ["Dr. Sharma ka appointment chahiye"], chk_unknown_doctor),
    Scenario("S12", "Symptom + booking is not escalated", "PAT-1011",
             ["Mujhe do din se bukhar hai, kal kisi general doctor ko dikhana hai"], chk_fever_booking),
    Scenario("S13", "Sunday request", "PAT-1015", ["Can I see Dr. Deshmukh on Sunday?"], chk_sunday),
    Scenario("S14", "Medical question only the LLM can catch", "PAT-1012",
             ["My sugar reading this morning was 250, is that okay?"], chk_escalated("URGENT")),
]


async def run_scenario(sc: Scenario, llm, pace: float) -> tuple[bool, str, list]:
    tmp = Path(tempfile.mkdtemp()) / "eval.db"
    conn = db.connect(tmp)
    seed.seed(conn, now=NOW)
    if sc.setup:
        sc.setup(conn)
    eng = DialogEngine(llm, now_fn=lambda: NOW)
    sess, _ = eng.start(conn, sc.patient)
    results, log = [], []
    for text in sc.turns:
        t0 = time.perf_counter()
        r = await eng.turn(conn, sess.id, text)
        ms = round((time.perf_counter() - t0) * 1000)
        results.append(r)
        log.append(f"    caller: {text}\n    bot   : [{r.lang}|{r.source}|{ms} ms|{','.join(r.tools) or '-'}"
                   f"|card={r.card['type'] if r.card else '-'}] {r.reply}")
        if pace:
            await asyncio.sleep(pace)
    errors = [r for r in results if r.source == "error"]
    failure = (f"assistant fell back to the error reply on {len(errors)} turn(s)" if errors
               else sc.check(results, conn, sess))
    conn.close()
    return failure is None, failure or "", log


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--pace", type=float, default=0.0)
    ap.add_argument("--model", default=None)
    ap.add_argument("--fallback", default=None)
    ap.add_argument("--provider", choices=["gemini", "groq"], default="gemini")
    args = ap.parse_args()
    if args.provider == "groq":
        llm = OpenAICompatLLM(settings.groq_api_key, args.model or settings.groq_model,
                              settings.groq_base_url, name="groq")
        llm.thinking = "-"
    else:
        llm = GeminiLLM(model=args.model, fallback_model=args.fallback)
    chosen = [s for s in SCENARIOS if not args.ids or s.id in args.ids]
    print(f"Model {llm.model} (thinking={llm.thinking}); now {NOW:%a %Y-%m-%d %H:%M}; {len(chosen)} scenarios\n")
    passed = 0
    for sc in chosen:
        ok, why, log = await run_scenario(sc, llm, args.pace)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {sc.id:4s} {sc.title}" + ("" if ok else f"  <- {why}"))
        print("\n".join(log))
    print(f"\n{passed}/{len(chosen)} scenarios passed")
    return 0 if passed == len(chosen) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
