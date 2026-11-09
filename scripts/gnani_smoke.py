"""Key check and cost calibration: prove the Gnani and Gemini keys work.

Usage (from the project root, after filling in .env):
    python scripts/gnani_smoke.py tts            # 5 TTS requests, saves WAVs
    python scripts/gnani_smoke.py stt            # transcribes the TTS WAVs (round trip)
    python scripts/gnani_smoke.py stt my.wav     # transcribes your own recording(s)
    python scripts/gnani_smoke.py gemini         # one function-calling request
    python scripts/gnani_smoke.py all --pause    # everything, pausing between groups

--pause waits for Enter before each group so you can note the credit balance
on https://app.gnani.ai/voice and work out the cost of STT and TTS separately.

Requests are sent strictly one at a time (Gnani accounts may allow only one
active request). Results are appended to smoke_out/report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "smoke_out"
load_dotenv(ROOT / ".env")

BASE = os.getenv("GNANI_BASE_URL", "https://api.vachana.ai").rstrip("/")
KEY = os.getenv("GNANI_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

KEYTERMS = ["Mahanadi Aarogya", "Dr. Aarav Chandrakar", "Dr. Meera Baghel",
            "Dr. Rohan Deshmukh", "Dr. Ananya Sahu", "appointment", "reschedule"]
TOPIC = ("Patient calling a small Indian clinic to book, cancel or reschedule a "
         "doctor appointment or ask about timings and fees.")

# (file stem, model, voice, language, text). Numbers are written as words,
# as the Timbre docs require.
TTS_CASES = [
    ("nalini_hi", "timbre-v2.5", "Nalini", "hi-IN",
     "नमस्ते, महानदी आरोग्य क्लिनिक में आपका स्वागत है। डॉक्टर चंद्राकर कल सुबह साढ़े दस बजे उपलब्ध हैं।"),
    ("kaveri_en", "timbre-v2.5", "Kaveri", "en-IN",
     "Hello, Doctor Chandrakar is available tomorrow at ten thirty in the morning. The fee is four hundred rupees."),
    ("poorvi_hinglish_roman", "timbre-v2.5", "Poorvi", "hi-en",
     "Ji haan, Doctor Chandrakar kal subah saadhe das baje available hain. Kya main appointment book kar doon?"),
    ("poorvi_hinglish_mixed", "timbre-v2.5", "Poorvi", "hi-en",
     "जी हाँ, Doctor Chandrakar कल सुबह साढ़े दस बजे available हैं। क्या main appointment book कर दूँ?"),
    ("shivani_v3_auto", "timbre-v3.0", "Shivani", "auto",
     "Ji haan, Doctor Chandrakar kal subah saadhe das baje available hain. Kya main appointment book kar doon?"),
]

STT_CONFIGS = [
    ("prisma-v3.0", {"model": "prisma-v3.0", "language_code": "hi-IN,en-IN",
                     "format": "transcribe", "keyterms": json.dumps(KEYTERMS),
                     "topic": TOPIC}),
    ("prisma-v2.5", {"language_code": "hi-IN", "format": "transcribe"}),
]


def need_key() -> None:
    if not KEY:
        sys.exit("GNANI_API_KEY is empty. Copy .env.example to .env and add your key.")


def record(entry: dict) -> None:
    OUT.mkdir(exist_ok=True)
    path = OUT / "report.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data.append(entry)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pause(enabled: bool, label: str) -> None:
    if enabled:
        input(f"\n>>> Note your credit balance now, then press Enter to run {label}... ")


def wav_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except (wave.Error, EOFError):
        return None


def run_tts(client: httpx.Client) -> None:
    need_key()
    OUT.mkdir(exist_ok=True)
    for stem, model, voice, lang, text in TTS_CASES:
        body = {"text": text, "model": model, "voice": voice, "language": lang,
                "speed": 1.0,
                "audio_config": {"sample_rate": 24000, "num_channels": 1,
                                 "sample_width": 2, "encoding": "linear_pcm",
                                 "container": "wav"}}
        t0 = time.perf_counter()
        r = client.post(f"{BASE}/api/v1/tts/inference", json=body,
                        headers={"X-API-Key-ID": KEY})
        ms = round((time.perf_counter() - t0) * 1000)
        entry = {"kind": "tts", "case": stem, "model": model, "voice": voice,
                 "language": lang, "chars": len(text), "status": r.status_code,
                 "latency_ms": ms}
        if r.status_code == 200 and r.content[:4] == b"RIFF":
            f = OUT / f"{stem}.wav"
            f.write_bytes(r.content)
            entry["file"] = str(f.relative_to(ROOT))
            entry["audio_seconds"] = wav_seconds(f)
            print(f"TTS  {stem:24s} 200  {ms:5d} ms  {len(text):3d} chars -> {f.name}")
        else:
            entry["error"] = r.text[:500]
            print(f"TTS  {stem:24s} {r.status_code}  {r.text[:200]}")
        record(entry)


def run_stt(client: httpx.Client, files: list[Path]) -> None:
    need_key()
    if not files:
        files = sorted(OUT.glob("*.wav"))
        if not files:
            sys.exit("No WAVs to transcribe. Run the tts step first or pass a file.")
    for f in files:
        audio = f.read_bytes()
        for label, fields in STT_CONFIGS:
            req_id = f"smoke-{uuid.uuid4().hex[:12]}"
            t0 = time.perf_counter()
            r = client.post(f"{BASE}/stt/v3", data=fields,
                            files={"audio_file": (f.name, audio, "audio/wav")},
                            headers={"X-API-Key-ID": KEY, "X-API-Request-ID": req_id})
            ms = round((time.perf_counter() - t0) * 1000)
            entry = {"kind": "stt", "file": f.name, "model": label,
                     "audio_seconds": wav_seconds(f), "status": r.status_code,
                     "latency_ms": ms}
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text[:500]}
            entry["response"] = body
            text = body.get("transcript") if isinstance(body, dict) else None
            lang = body.get("detected_language_code") if isinstance(body, dict) else None
            print(f"STT  {f.name:28s} {label:12s} {r.status_code}  {ms:5d} ms  "
                  f"[{lang}] {text if text is not None else str(body)[:200]}")
            record(entry)


def run_gemini() -> None:
    if not GEMINI_KEY:
        sys.exit("GEMINI_API_KEY is empty in .env.")
    from google import genai
    from google.genai import types

    tool = types.Tool(function_declarations=[types.FunctionDeclaration(
        name="check_slots",
        description="Find open appointment slots for a doctor on a date.",
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "doctor": types.Schema(type=types.Type.STRING),
            "date": types.Schema(type=types.Type.STRING, description="YYYY-MM-DD"),
        }, required=["doctor", "date"]),
    )])
    client = genai.Client(api_key=GEMINI_KEY)
    t0 = time.perf_counter()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents="Today is 2026-11-09. Caller: Kal subah Dr. Chandrakar ka slot khali hai kya?",
        config=types.GenerateContentConfig(
            tools=[tool],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
    )
    ms = round((time.perf_counter() - t0) * 1000)
    calls = [{"name": c.name, "args": dict(c.args or {})} for c in (resp.function_calls or [])]
    print(f"GEMINI {GEMINI_MODEL}  {ms} ms  function_calls={calls}  text={resp.text if not calls else ''}")
    record({"kind": "gemini", "model": GEMINI_MODEL, "latency_ms": ms, "function_calls": calls})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["tts", "stt", "gemini", "all"])
    ap.add_argument("files", nargs="*", type=Path, help="WAV files for the stt step")
    ap.add_argument("--pause", action="store_true", help="wait for Enter before each group")
    args = ap.parse_args()

    with httpx.Client(timeout=30) as client:
        if args.step in ("tts", "all"):
            pause(args.pause, "TTS")
            run_tts(client)
        if args.step in ("stt", "all"):
            pause(args.pause, "STT")
            run_stt(client, args.files)
    if args.step in ("gemini", "all"):
        run_gemini()
    if args.pause:
        input("\n>>> Note the final credit balance, then press Enter to finish. ")
    print(f"\nReport: {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
