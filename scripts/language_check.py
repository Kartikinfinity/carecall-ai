"""Live check of every supported language against the real services.

    python scripts/language_check.py                 # speech round trip + a booking, all languages
    python scripts/language_check.py ta-IN bn-IN     # selected languages
    python scripts/language_check.py --speech-only   # skip the LLM part
    python scripts/language_check.py --pace 5        # wait between LLM turns (free-tier limits)

Two checks per language:
1. Speech round trip. Timbre speaks a sentence in the language's voice, then
   Prisma transcribes that audio. The transcript must come back in the same
   script and be close to the original. This needs no native speaker.
2. Booking. The same three steps the browser sends (ask, pick the first slot,
   press Confirm) go through the real LLM. Every reply must be in the caller's
   language and the appointment must exist in the database afterwards.

Uses a few Gnani credits per language (two short TTS clips are cached on disk).
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app import db, languages, phrases, seed            # noqa: E402
from app.config import now_ist                          # noqa: E402
from app.dialog import DialogEngine                     # noqa: E402
from app.gnani import GnaniClient, GnaniError           # noqa: E402
from app.llm import GeminiLLM                           # noqa: E402
from app.safety import guess_lang                       # noqa: E402
from app.tts_text import normalize                      # noqa: E402

NOW = now_ist()
PATIENT = "PAT-1020"

# "I need an appointment with Doctor Chandrakar tomorrow."
ASK = {
    "hi-en": "Kal Doctor Chandrakar ka appointment chahiye.",
    "hi-IN": "मुझे कल डॉक्टर चंद्राकर का अपॉइंटमेंट चाहिए।",
    "en-IN": "I need an appointment with Doctor Chandrakar tomorrow.",
    "mr-IN": "मला उद्या डॉक्टर चंद्राकर यांची अपॉइंटमेंट हवी आहे.",
    "bn-IN": "আমি কাল ডাক্তার চন্দ্রাকরের অ্যাপয়েন্টমেন্ট চাই।",
    "gu-IN": "મારે કાલે ડૉક્ટર ચંદ્રાકરની એપોઇન્ટમેન્ટ જોઈએ છે.",
    "pa-IN": "ਮੈਨੂੰ ਕੱਲ੍ਹ ਡਾਕਟਰ ਚੰਦਰਾਕਰ ਦੀ ਅਪਾਇੰਟਮੈਂਟ ਚਾਹੀਦੀ ਹੈ।",
    "ta-IN": "நாளைக்கு டாக்டர் சந்திராகர் அப்பாயிண்ட்மெண்ட் வேண்டும்.",
    "te-IN": "రేపు డాక్టర్ చంద్రాకర్ అపాయింట్‌మెంట్ కావాలి.",
    "kn-IN": "ನಾಳೆ ಡಾಕ್ಟರ್ ಚಂದ್ರಾಕರ್ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ಬೇಕು.",
    "ml-IN": "നാളെ ഡോക്ടർ ചന്ദ്രാകറിന്റെ അപ്പോയിന്റ്മെന്റ് വേണം.",
}


def _letters(text: str) -> str:
    return re.sub(r"[\W\d_]+", "", languages.strip_joiners(text)).lower()


async def speech_round_trip(client: GnaniClient, code: str) -> tuple[bool, str]:
    said = phrases.reply("REPEAT", code)
    try:
        tts = await client.synthesize(normalize(said, code), code)
        stt = await client.transcribe(tts.audio, language_code=languages.stt_codes(code))
    except GnaniError as exc:
        return False, f"Gnani error ({exc.kind}): {exc}"
    heard_lang = guess_lang(stt.text, fallback=code)
    close = difflib.SequenceMatcher(None, _letters(said), _letters(stt.text)).ratio()
    hindi_family = {"hi-en", "hi-IN"}          # Prisma writes Hinglish back in Devanagari
    same = heard_lang == code or {heard_lang, code} <= hindi_family
    ok = same and close >= 0.6
    return ok, (f"voice {tts.voice} ({tts.latency_ms} ms) -> Prisma tagged {stt.language}, "
                f"read as {heard_lang}, {close:.0%} match ({stt.latency_ms} ms)\n      said : {said}\n      heard: {stt.text}")


async def booking(llm, code: str, pace: float) -> tuple[bool, str]:
    conn = db.connect(Path(tempfile.mkdtemp()) / "check.db")
    seed.seed(conn, now=NOW)
    eng = DialogEngine(llm, now_fn=lambda: NOW)
    sess, _ = eng.start(conn, PATIENT, code)
    buttons, log, replies = phrases.buttons(code), [], []

    async def say(text: str, confirm: bool = False):
        r = await eng.turn(conn, sess.id, text, confirm_click=confirm)
        replies.append(r)
        log.append(f"      caller: {text}\n      bot   : [{r.lang}|{r.source}|{','.join(r.tools) or '-'}] {r.reply}")
        if pace:
            await asyncio.sleep(pace)
        return r

    first = await say(ASK[code])
    slots = (first.card or {}).get("slots") or []
    if slots:
        s = slots[0]
        await say(buttons["slot"].format(date=s["date"], time=s["time"], weekday=s.get("weekday", "")))
        await say(buttons["confirm"], confirm=True)
    booked = conn.execute("SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND source = 'AI'"
                          " AND status = 'CONFIRMED'", (PATIENT,)).fetchone()[0]
    conn.close()
    wrong_lang = [r.lang for r in replies if r.lang != code]
    errors = [r.source for r in replies if r.source in ("error", "busy")]
    problems = ([] if slots else ["no slots offered on the first turn"]) + \
               ([] if booked else ["no booking in the database"]) + \
               ([f"replied in {sorted(set(wrong_lang))}"] if wrong_lang else []) + \
               ([f"fallback replies: {errors}"] if errors else [])
    return not problems, ("; ".join(problems) or "booked, every reply in the caller's language") + "\n" + "\n".join(log)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--speech-only", action="store_true")
    ap.add_argument("--dialog-only", action="store_true")
    ap.add_argument("--pace", type=float, default=4.0)
    args = ap.parse_args()
    chosen = [c for c in languages.codes() if not args.codes or c in args.codes]
    client = GnaniClient()
    llm = None if args.speech_only else GeminiLLM()
    failed = []
    for code in chosen:
        name = languages.get(code).name
        if not args.dialog_only:
            ok, detail = await speech_round_trip(client, code)
            print(f"{'PASS' if ok else 'FAIL'}  {code}  {name:10s} speech   {detail}")
            failed += [] if ok else [f"{code} speech"]
        if llm:
            ok, detail = await booking(llm, code, args.pace)
            print(f"{'PASS' if ok else 'FAIL'}  {code}  {name:10s} booking  {detail}")
            failed += [] if ok else [f"{code} booking"]
    await client.aclose()
    print(f"\n{len(failed)} failure(s)" + (": " + ", ".join(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
