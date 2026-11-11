"""A live phone conversation: Gnani streaming speech-to-text in, Timbre speech out.

    caller audio (mu-law) -> Prisma streaming (8 kHz, Gnani decides when the caller has
    finished a sentence) -> the same DialogEngine the browser uses -> Timbre (mu-law) -> caller

Because the dialog engine is reused as it is, every rule holds on the phone too:
emergencies skip the LLM, nothing is booked without a clear yes, and the
caller's phone number is never read, stored or shown.

Barge-in: if the caller starts talking while the receptionist is speaking, the
audio already queued at the phone provider is cleared so she stops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol

from . import languages, phrases
from .audio import ulaw_seconds, ulaw_to_pcm16
from .config import Settings, settings as default_settings
from .dialog import DialogEngine
from .gnani import GnaniClient, GnaniError
from .tts_text import normalize

log = logging.getLogger("carecall.phone")

STT_STREAM_URL = "wss://api.vachana.ai/stt/v3/stream"
FRAME_BYTES = 1024                 # Gnani wants exactly 512 samples per frame
MAX_CALL_SECONDS = 600             # hang on to a call for at most ten minutes
HOLD_AFTER_SECONDS = 1.5           # say "one moment" if the answer takes longer than this


# ------------------------------------------------------------------ speech-to-text stream

@dataclass
class SttEvent:
    kind: str                      # "speech_start" | "transcript" | "error"
    text: str = ""
    language: str | None = None
    request_id: str | None = None
    latency_ms: int | None = None


class SttStream:
    """One Gnani Prisma streaming session for one call."""

    def __init__(self, cfg: Settings, language_code: str, sample_rate: int = 8000, connect=None):
        self.cfg, self.language_code, self.sample_rate = cfg, language_code, sample_rate
        self._connect = connect
        self._ws = None
        self._buffer = bytearray()

    def headers(self) -> dict:
        codes = [c.strip() for c in self.language_code.split(",") if c.strip()]
        head = {"x-api-key-id": self.cfg.gnani_api_key, "model": self.cfg.gnani_stt_model, "format": "transcribe",
                "language_code": codes[0], "x-sample-rate": str(self.sample_rate), "x-min-silence-ms": "600"}
        if len(codes) > 1:          # e.g. Hindi with English words kept in Latin letters
            head["multi_lang_codes"] = ",".join(codes[:languages.MAX_STT_CODES])
        return head

    async def open(self) -> "SttStream":
        connect = self._connect
        if connect is None:
            import websockets
            connect = websockets.connect
        self._ws = await connect(STT_STREAM_URL, additional_headers=self.headers())
        return self

    async def send_pcm(self, pcm: bytes) -> None:
        self._buffer += pcm
        while len(self._buffer) >= FRAME_BYTES:
            frame, self._buffer = bytes(self._buffer[:FRAME_BYTES]), self._buffer[FRAME_BYTES:]
            await self._ws.send(frame)

    async def events(self) -> AsyncIterator[SttEvent]:
        try:
            async for message in self._ws:
                if not isinstance(message, str):
                    continue
                data = json.loads(message)
                kind = data.get("type")
                if kind == "speech_start":
                    yield SttEvent("speech_start")
                elif kind == "transcript":
                    yield SttEvent("transcript", (data.get("text") or "").strip(), data.get("detected_language_code"),
                                   data.get("segment_id"), data.get("latency"))
                elif kind == "error":
                    yield SttEvent("error", data.get("message") or "")
        except Exception as exc:  # noqa: BLE001  (a dropped socket just ends the call's listening)
            yield SttEvent("error", repr(exc))

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass


async def open_stt(cfg: Settings, language_code: str) -> SttStream:
    return await SttStream(cfg, language_code).open()


# ------------------------------------------------------------------ the call

class PhoneOut(Protocol):
    """What a phone provider adapter must be able to do with the caller's line."""

    async def play(self, ulaw: bytes) -> None: ...
    async def clear(self) -> None: ...
    async def transfer(self) -> bool: ...


class PhoneCall:
    def __init__(self, engine: DialogEngine, gnani: GnaniClient, conn, out: PhoneOut,
                 stt_factory: Callable[[Settings, str], Awaitable[SttStream]] = open_stt,
                 cfg: Settings = default_settings, lang: str | None = None,
                 hold_after: float = HOLD_AFTER_SECONDS, clock=time.monotonic):
        self.engine, self.gnani, self.conn, self.out = engine, gnani, conn, out
        self.stt_factory, self.cfg, self.hold_after, self.clock = stt_factory, cfg, hold_after, clock
        self.auto = lang in (None, "", languages.AUTO)
        self.lang = languages.get(None if self.auto else lang).code
        self.speaking_until = 0.0
        self.turns = 0
        self.transferred = False
        self.sess = None

    # ---------------------------------------------------------------- speaking

    def speaking(self) -> bool:
        return self.clock() < self.speaking_until

    async def say(self, text: str, lang: str, log_turn: bool = True) -> None:
        try:
            tts = await self.gnani.synthesize(normalize(text, lang), lang, audio_format="mulaw")
        except GnaniError as exc:
            log.warning("phone TTS failed: %s", exc)
            return
        if log_turn and self.sess is not None:
            self.engine.log_tts(self.conn, self.sess, tts.request_id, tts.latency_ms)
        await self.out.play(tts.audio)
        self.speaking_until = max(self.clock(), self.speaking_until) + ulaw_seconds(tts.audio)

    async def _hold(self, lang: str) -> None:
        await asyncio.sleep(self.hold_after)
        await self.say(phrases.reply("HOLD", lang), lang, log_turn=False)

    # ---------------------------------------------------------------- the conversation

    async def run(self, inbound: AsyncIterator[bytes]) -> None:
        """Run until the caller hangs up (the inbound audio ends) or the time limit passes."""
        self.sess, greeting = self.engine.start(self.conn, None, self.lang, auto=self.auto)
        self.conn.execute("UPDATE calls SET channel = 'PHONE' WHERE id = ?", (self.sess.call_id,))
        stt = await self.stt_factory(self.cfg, languages.stt_codes(self.lang, self.auto, self.cfg.gnani_stt_langs))
        pump = asyncio.create_task(self._pump(inbound, stt))
        try:
            await self.say(greeting, self.sess.lang)
            async with asyncio.timeout(MAX_CALL_SECONDS):
                async for event in stt.events():
                    if event.kind == "speech_start":
                        if self.speaking():                 # the caller is talking over her: stop
                            await self.out.clear()
                            self.speaking_until = 0.0
                    elif event.kind == "transcript" and event.text:
                        await self._turn(event)
                        if self.transferred:
                            break
                    elif event.kind == "error":
                        log.warning("phone STT: %s", event.text)
                        break
        except TimeoutError:
            pass
        finally:
            pump.cancel()
            await stt.close()
            self.engine.end(self.conn, self.sess.id)

    async def _pump(self, inbound: AsyncIterator[bytes], stt: SttStream) -> None:
        try:
            async for chunk in inbound:
                await stt.send_pcm(ulaw_to_pcm16(chunk))
        finally:
            await stt.close()        # the caller hung up: this ends stt.events()

    async def _turn(self, event: SttEvent) -> None:
        self.turns += 1
        hold = asyncio.create_task(self._hold(self.sess.lang))
        try:
            result = await self.engine.turn(self.conn, self.sess.id, event.text, stt={
                "request_id": event.request_id, "latency_ms": event.latency_ms, "language": event.language})
        finally:
            hold.cancel()
        await self.say(result.reply, result.lang)
        if result.escalated and result.source == "safety":
            # Emergency, a medical question or "let me talk to a person": try to bring a human on the line.
            await asyncio.sleep(max(0.0, self.speaking_until - self.clock()))
            self.transferred = await self.out.transfer()
