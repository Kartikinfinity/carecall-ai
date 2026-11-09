"""Live check: one Timbre TTS call and one Prisma STT call through app.gnani.

    python scripts/client_check.py

Speaks a normalized Hinglish reply with the default voice, saves it to
smoke_out/client_check.wav, then transcribes that audio back.
"""
from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db                                  # noqa: E402
from app.config import ROOT, settings               # noqa: E402
from app.gnani import GnaniClient, keyterms_from_db, wav_seconds  # noqa: E402
from app.tts_text import normalize                  # noqa: E402

REPLY = "जी हाँ, Dr. Chandrakar कल 10:30 बजे available हैं, fee ₹400 है। क्या मैं book कर दूँ?"


async def main() -> None:
    client = GnaniClient()
    conn = db.connect(settings.database_path)
    spoken = normalize(REPLY, "hi-en")
    print("LLM text :", REPLY)
    print("TTS text :", spoken)
    tts = await client.synthesize(spoken, "hi-en")
    out = ROOT / "smoke_out" / "client_check.wav"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(tts.audio)
    riff = struct.unpack_from("<I", tts.audio, 4)[0]
    print(f"TTS      : {tts.model}/{tts.voice} {tts.latency_ms} ms cached={tts.cached} "
          f"{wav_seconds(tts.audio):.2f}s header_ok={riff == len(tts.audio) - 8} -> {out.name}")
    stt = await client.transcribe(tts.audio, keyterms=keyterms_from_db(conn))
    print(f"STT      : {settings.gnani_stt_model} {stt.latency_ms} ms [{stt.language}] "
          f"id={stt.request_id}\n           {stt.text}")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
