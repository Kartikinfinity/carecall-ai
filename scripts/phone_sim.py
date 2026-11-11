"""Simulate a phone call against the real server code, real Gnani speech and the real LLM.

    python scripts/phone_sim.py                 # one Hinglish request
    python scripts/phone_sim.py "Clinic kitne baje khulta hai?"

It behaves like a phone provider (Twilio's message format): it opens the audio
stream, "speaks" a sentence in 20 ms mu-law packets at real-time speed, and
records what comes back. The caller's sentence is spoken by Timbre, so no
microphone is needed. Prints how long the caller waits for the reply, and what
the receptionist said (transcribed back with Prisma). Uses a temporary database.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TOKEN = "sim-" + os.urandom(6).hex()
os.environ["PHONE_TOKEN"] = TOKEN
os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "phone_sim.db")
os.environ.pop("ACCESS_CODE", None)

import uvicorn                                   # noqa: E402
import websockets                                # noqa: E402

from app.audio import ulaw_to_pcm16              # noqa: E402
from app.gnani import GnaniClient                # noqa: E402
from app.main import app                         # noqa: E402

PORT = 8765
PACKET = 160                                     # 20 ms of 8 kHz mu-law
SAY = sys.argv[1] if len(sys.argv) > 1 else "Kal subah Doctor Chandrakar ka appointment chahiye."


def to_wav(ulaw: bytes) -> bytes:
    import io
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(ulaw_to_pcm16(ulaw))
    return out.getvalue()


async def main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    gnani = GnaniClient()
    caller = (await gnani.synthesize(SAY, "hi-en", audio_format="mulaw")).audio
    print(f"caller will say ({len(caller) / 8000:.1f} s): {SAY}")

    got: list[tuple[float, bytes]] = []          # (time received, audio)
    t0 = time.perf_counter()
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/phone/stream") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "streamSid": "SIM1", "start": {
            "streamSid": "SIM1", "callSid": "SIMCALL", "customParameters": {"token": TOKEN}}}))

        async def listen():
            async for raw in ws:
                msg = json.loads(raw)
                if msg["event"] == "media":
                    got.append((time.perf_counter() - t0, base64.b64decode(msg["media"]["payload"])))

        listener = asyncio.create_task(listen())

        async def send(ulaw: bytes):
            for i in range(0, len(ulaw), PACKET):
                await ws.send(json.dumps({"event": "media", "streamSid": "SIM1", "media": {
                    "track": "inbound", "payload": base64.b64encode(ulaw[i:i + PACKET].ljust(PACKET, b"\xff")).decode()}}))
                await asyncio.sleep(0.02)

        await send(b"\xff" * 8000 * 6)           # stay quiet while the greeting plays
        greeting_parts = len(got)
        await send(caller)
        spoke_until = time.perf_counter() - t0
        await send(b"\xff" * 8000 * 22)          # silence while we wait for the answer
        await ws.send(json.dumps({"event": "stop", "streamSid": "SIM1"}))
        await asyncio.sleep(0.5)
        listener.cancel()

    greeting = b"".join(a for _, a in got[:greeting_parts])
    replies = got[greeting_parts:]
    print(f"greeting: {len(greeting) / 8000:.1f} s of audio, first packet after {got[0][0]:.2f} s" if got else "NO GREETING")
    if replies:
        first = replies[0][0] - spoke_until
        print(f"reply started {first:.2f} s after the caller stopped speaking; {len(replies)} audio message(s)")
        # Split on gaps so a "one moment" filler and the real answer are reported separately.
        groups, last = [], None
        for t, audio in replies:
            if last is None or t - last > 0.6:
                groups.append([t, b""])
            groups[-1][1] += audio
            last = t
        for t, audio in groups:
            text = (await gnani.transcribe(to_wav(audio), language_code="hi-IN,en-IN")).text
            print(f"  +{t - spoke_until:5.2f} s  ({len(audio) / 8000:.1f} s)  {text}")
    else:
        print("NO REPLY AUDIO")
    await gnani.aclose()
    server.should_exit = True
    await serving


if __name__ == "__main__":
    asyncio.run(main())
