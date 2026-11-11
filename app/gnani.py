"""Gnani Speech API client: Prisma STT and Timbre TTS over REST.

Reliability rules (all from docs.gnani.ai):
- One request at a time: accounts can be limited to "1 active request" (HTTP 429),
  so every call goes through one asyncio.Lock.
- Retry connection errors and 429/500/503/504 with 0.5 s / 1 s / 2 s backoff;
  never retry other 4xx (they fail the same way again).
- Timbre's WAV header carries 0xFFFFFFFF size fields (seen in testing); we rewrite
  the sizes so every browser reports and plays the clip correctly.
- TTS results are cached on disk, so repeated prompts cost no credits.
- GNANI_MOCK=1 returns canned audio/transcripts for development and tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import struct
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from . import languages
from .config import ROOT, Settings, now_ist, settings as default_settings

RETRY_STATUSES = {429, 500, 503, 504}
RETRY_DELAYS = (0.5, 1.0, 2.0)
QUEUE_TIMEOUT_S = 20.0
MAX_STT_SECONDS = 30.0
MIN_STT_SECONDS = 0.1
STT_TOPIC = ("Patient calling a small Indian clinic to book, cancel or reschedule a doctor "
             "appointment, or to ask about clinic timings, fees and tests.")

# Voice per reply language, from the language table. Timbre v3.0 voices speak every
# language, so one voice with language "auto" is enough there.
VOICES = {
    "timbre-v2.5": {l.code: (l.voice, l.tts_code) for l in languages.enabled()},
    "timbre-v3.0": {l.code: ("Shivani", "auto") for l in languages.enabled()},
}


class GnaniError(Exception):
    """kind: auth | credits | scope | rate | busy | cap | bad_request | server | network | audio"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status

    @property
    def user_message(self) -> str:
        return {
            "credits": "Speech service unavailable: Gnani credits are exhausted.",
            "scope": "Speech service unavailable: this API key lacks STT/TTS access.",
            "auth": "Speech service unavailable: the Gnani API key was rejected.",
            "cap": "Daily speech limit for this demo has been reached.",
            "audio": "That recording could not be used. Please try again.",
        }.get(self.kind, "Speech service is busy or unreachable. Please try again.")


@dataclass
class SttResult:
    text: str
    language: str | None
    request_id: str
    latency_ms: int
    truncated: bool = False
    audio_seconds: float = 0.0


@dataclass
class TtsResult:
    audio: bytes            # complete WAV file with correct header
    voice: str
    model: str
    language: str
    latency_ms: int
    cached: bool
    request_id: str


@dataclass
class _DailyCounter:
    day: date | None = None
    used: int = 0


# ----------------------------------------------------------------------- WAV helpers

def fix_wav_header(data: bytes) -> bytes:
    """Rewrite RIFF and data chunk sizes to match the actual byte length."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    buf = bytearray(data)
    pos = 12
    while pos + 8 <= len(buf):
        chunk_id = bytes(buf[pos:pos + 4])
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        if chunk_id == b"data":
            struct.pack_into("<I", buf, pos + 4, len(buf) - pos - 8)
            break
        pos += 8 + size + (size & 1)
    struct.pack_into("<I", buf, 4, len(buf) - 8)
    return bytes(buf)


def wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise GnaniError("audio", f"Not a valid WAV file: {exc}") from exc


def tone_wav(seconds: float = 0.6, rate: int = 24000, freq: float = 660.0) -> bytes:
    """A short soft beep, used as mock TTS audio."""
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate)))
                      for i in range(n))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return out.getvalue()


def sanitize_keyterms(terms: list[str], limit: int = 200) -> list[str]:
    """Strip reserved <| |> markers, drop blanks and duplicates, cap at the API limit."""
    seen, out = set(), []
    for t in terms:
        t = t.replace("<|", "").replace("|>", "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


# ----------------------------------------------------------------------- client

@dataclass
class GnaniClient:
    cfg: Settings = field(default_factory=lambda: default_settings)
    transport: httpx.AsyncBaseTransport | None = None     # injected in tests
    cache_dir: Path = ROOT / "data" / "tts_cache"
    sleep: callable = asyncio.sleep                         # injected in tests
    mock_transcript: str = "Kal subah Doctor Chandrakar ka appointment chahiye."

    def __post_init__(self):
        self._lock = asyncio.Lock()
        self._counter = _DailyCounter()
        self._http: httpx.AsyncClient | None = None

    @property
    def mock(self) -> bool:
        return self.cfg.gnani_mock

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.cfg.gnani_base_url, transport=self.transport,
                                           timeout=httpx.Timeout(20.0, connect=5.0))
        return self._http

    def calls_today(self) -> int:
        return self._counter.used if self._counter.day == now_ist().date() else 0

    def _count(self) -> None:
        today = now_ist().date()
        if self._counter.day != today:
            self._counter = _DailyCounter(today, 0)
        if self.cfg.daily_gnani_cap and self._counter.used >= self.cfg.daily_gnani_cap:
            raise GnaniError("cap", "Daily Gnani request cap reached")
        self._counter.used += 1

    async def _send(self, build_request) -> httpx.Response:
        """Serialized, retried request. `build_request` returns kwargs for httpx.request."""
        if not self.cfg.gnani_api_key:
            raise GnaniError("auth", "GNANI_API_KEY is not set")
        try:
            await asyncio.wait_for(self._lock.acquire(), QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise GnaniError("busy", "Timed out waiting for the speech service queue") from exc
        try:
            self._count()
            last: GnaniError | None = None
            for attempt in range(len(RETRY_DELAYS) + 1):
                if attempt:
                    await self.sleep(RETRY_DELAYS[attempt - 1])
                try:
                    resp = await self._client().request(**build_request())
                except httpx.TransportError as exc:
                    last = GnaniError("network", f"Network error: {exc!r}")
                    continue
                if resp.status_code < 400:
                    return resp
                last = _error_from(resp)
                if resp.status_code not in RETRY_STATUSES:
                    raise last
            raise last
        finally:
            self._lock.release()

    # ------------------------------------------------------------------- STT

    async def transcribe(self, wav: bytes, keyterms: list[str] | None = None,
                         previous_text: str | None = None, language_code: str | None = None) -> SttResult:
        """`language_code`: Prisma codes for this call (see languages.stt_codes); default from settings."""
        seconds = wav_seconds(wav)
        request_id = f"cc-{uuid.uuid4().hex[:16]}"
        if seconds > MAX_STT_SECONDS:
            raise GnaniError("audio", f"Recording is {seconds:.1f}s; the limit is 30s")
        if seconds < MIN_STT_SECONDS:
            return SttResult("", None, request_id, 0, audio_seconds=seconds)
        if self.mock:
            return SttResult(self.mock_transcript, "hi-IN", f"mock-{request_id}", 5, audio_seconds=seconds)

        fields = {"model": self.cfg.gnani_stt_model, "language_code": language_code or self.cfg.gnani_stt_langs,
                  "format": "transcribe", "topic": STT_TOPIC}
        terms = sanitize_keyterms(keyterms or [])
        if terms and self.cfg.gnani_stt_model == "prisma-v3.0":
            fields["keyterms"] = json.dumps(terms, ensure_ascii=False)
        prev = (previous_text or "").replace("<|", "").replace("|>", "").strip()
        if prev:
            fields["previous_text"] = prev[-600:]

        t0 = time.perf_counter()
        resp = await self._send(lambda: {
            "method": "POST", "url": "/stt/v3", "data": fields,
            "files": {"audio_file": ("turn.wav", wav, "audio/wav")},
            "headers": {"X-API-Key-ID": self.cfg.gnani_api_key, "X-API-Request-ID": request_id}})
        body = resp.json()
        return SttResult(text=(body.get("transcript") or "").strip(),
                         language=body.get("detected_language_code"),
                         request_id=body.get("request_id") or request_id,
                         latency_ms=round((time.perf_counter() - t0) * 1000),
                         truncated=bool(body.get("truncated")), audio_seconds=seconds)

    # ------------------------------------------------------------------- TTS

    def voice_for(self, lang: str) -> tuple[str, str]:
        voices = VOICES.get(self.cfg.gnani_tts_model, VOICES["timbre-v2.5"])
        return voices.get(lang, voices[languages.DEFAULT])

    def _cache_path(self, model: str, voice: str, lang: str, text: str) -> Path:
        key = hashlib.sha256(f"{model}|{voice}|{lang}|{text}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.wav"

    async def synthesize(self, text: str, lang: str = "hi-en", audio_format: str = "wav") -> TtsResult:
        """`text` must already be normalized to spoken words (see tts_text.normalize).

        audio_format "wav" is a 24 kHz WAV for the browser; "mulaw" is raw 8 kHz mu-law for a phone line.
        """
        phone = audio_format == "mulaw"
        text = text.strip()
        if not text:
            raise GnaniError("bad_request", "Nothing to speak")
        model = self.cfg.gnani_tts_model
        voice, api_lang = self.voice_for(lang)
        request_id = f"cc-{uuid.uuid4().hex[:16]}"
        if self.mock:
            audio = b"\xff" * 1600 if phone else tone_wav()          # 0.2 s of silence on a phone line
            return TtsResult(audio, voice, model, api_lang, 5, False, f"mock-{request_id}")

        path = self._cache_path(model, voice, api_lang, text)
        if phone:
            path = path.with_suffix(".ulaw")
        if path.exists():
            return TtsResult(path.read_bytes(), voice, model, api_lang, 0, True, "cache")

        body = {"text": text, "model": model, "voice": voice, "language": api_lang, "speed": 1.0,
                "audio_config": {"sample_rate": 24000, "num_channels": 1, "sample_width": 2,
                                 "encoding": "linear_pcm", "container": "wav"}}
        if phone:
            body["audio_config"] = {"sample_rate": 8000, "num_channels": 1, "sample_width": 2, "container": "mulaw"}
        t0 = time.perf_counter()
        resp = await self._send(lambda: {
            "method": "POST", "url": "/api/v1/tts/inference", "json": body,
            "headers": {"X-API-Key-ID": self.cfg.gnani_api_key, "X-API-Request-ID": request_id}})
        audio = resp.content
        if phone:
            if not audio:
                raise GnaniError("server", "TTS returned no audio")
        elif audio[:4] != b"RIFF":
            raise GnaniError("server", "TTS did not return WAV audio")
        else:
            audio = fix_wav_header(audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return TtsResult(audio, voice, model, api_lang, round((time.perf_counter() - t0) * 1000),
                         False, request_id)


def _error_from(resp: httpx.Response) -> GnaniError:
    status = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error_code") or ""
    elif isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = f"{body['error'].get('type')}: {body['error'].get('message')}"
    elif isinstance(body, dict):
        message = body.get("message") or body.get("code") or resp.text[:300]
    else:
        message = resp.text[:300]
    kind = {401: "auth", 402: "credits", 403: "scope", 429: "rate"}.get(status)
    if kind is None:
        kind = "server" if status >= 500 else "bad_request"
    return GnaniError(kind, f"HTTP {status}: {message}", status)


def keyterms_from_db(conn) -> list[str]:
    """Short, distinctive terms only: full names as keyterms made Prisma insert words nobody said."""
    terms = ["Mahanadi Aarogya", "appointment", "reschedule", "cancel", "ultrasound",
             "orthopaedic", "pediatric", "ECG", "X-ray", "blood test"]
    for row in conn.execute("SELECT name FROM doctors"):
        terms.append(row["name"].split()[-1])          # surname: "Chandrakar"
    return sanitize_keyterms(terms)
