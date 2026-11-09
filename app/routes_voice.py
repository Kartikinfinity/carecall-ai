"""Patient-facing API.

A spoken turn is two requests so the caller sees their own words early:
  1. POST /api/transcribe  (WAV -> Gnani Prisma)    -> transcript
  2. POST /api/turn        (transcript -> dialog -> Gnani Timbre) -> reply + audio
Typed text skips step 1.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import booking
from .dialog import DialogEngine, SessionNotFound
from .gnani import GnaniClient, GnaniError, keyterms_from_db
from .tts_text import normalize

router = APIRouter(prefix="/api")


def engine(request: Request) -> DialogEngine:
    return request.app.state.engine


def gnani(request: Request) -> GnaniClient:
    return request.app.state.gnani


def get_conn(request: Request):
    from .main import get_conn as _get_conn          # one connection per request
    yield from _get_conn()


class StartIn(BaseModel):
    patient_id: str | None = None
    lang: str | None = None
    speak: bool = True


class TurnIn(BaseModel):
    session_id: str
    text: str = Field("", max_length=1000)
    confirm: bool = False
    speak: bool = True
    stt: dict | None = None          # metadata from /api/transcribe, logged with the turn

MAX_UPLOAD_BYTES = 2_000_000         # 30 s of 16 kHz mono 16-bit PCM is ~960 KB


async def _speak(conn, eng: DialogEngine, client: GnaniClient, sess, text: str, lang: str) -> dict:
    """Synthesize with Gnani Timbre. On failure the caller still gets the text."""
    try:
        tts = await client.synthesize(normalize(text, lang), lang)
    except GnaniError as exc:
        return {"audio_b64": None, "tts_error": exc.user_message}
    eng.log_tts(conn, sess, tts.request_id, tts.latency_ms)
    return {"audio_b64": base64.b64encode(tts.audio).decode("ascii"),
            "tts": {"model": tts.model, "voice": tts.voice, "language": tts.language,
                    "latency_ms": tts.latency_ms, "cached": tts.cached, "request_id": tts.request_id}}


@router.get("/patients")
def patients(conn=Depends(get_conn)) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, preferred_lang, demo_contact FROM patients ORDER BY id")]


@router.post("/session")
async def start(body: StartIn, conn=Depends(get_conn), eng=Depends(engine), client=Depends(gnani)) -> dict:
    try:
        sess, greeting = eng.start(conn, body.patient_id, body.lang)
    except booking.NotFound as exc:
        raise HTTPException(404, str(exc))
    out = {"session_id": sess.id, "patient_id": sess.patient_id, "patient_name": sess.patient_name,
           "reply": greeting, "lang": sess.lang}
    if body.speak:
        out |= await _speak(conn, eng, client, sess, greeting, sess.lang)
    return out


@router.post("/transcribe")
async def transcribe(session_id: str = Form(...), audio: UploadFile = File(...), conn=Depends(get_conn),
                     eng=Depends(engine), client=Depends(gnani)) -> dict:
    try:
        sess = eng.get(session_id)
    except SessionNotFound:
        raise HTTPException(404, "Call session not found or already ended. Start a new call.")
    wav = await audio.read(MAX_UPLOAD_BYTES + 1)
    if len(wav) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Recording is too long; please keep it under 25 seconds.")
    try:
        res = await client.transcribe(wav, keyterms=keyterms_from_db(conn), previous_text=sess.last_bot_text)
    except GnaniError as exc:
        status = 422 if exc.kind == "audio" else 503
        raise HTTPException(status, exc.user_message)
    return {"text": res.text, "language": res.language, "request_id": res.request_id,
            "latency_ms": res.latency_ms, "truncated": res.truncated,
            "audio_seconds": round(res.audio_seconds, 2), "model": client.cfg.gnani_stt_model}


@router.post("/turn")
async def turn(body: TurnIn, conn=Depends(get_conn), eng=Depends(engine), client=Depends(gnani)) -> dict:
    waits_before = getattr(eng.llm, "rate_limit_waits", 0)
    try:
        stt = {k: body.stt.get(k) for k in ("request_id", "latency_ms")} if body.stt else None
        result = await eng.turn(conn, body.session_id, body.text, confirm_click=body.confirm, stt=stt)
        sess = eng.get(body.session_id)
    except SessionNotFound:
        raise HTTPException(404, "Call session not found or already ended. Start a new call.")
    out = {"user_text": body.text, "reply": result.reply, "lang": result.lang, "card": result.card,
           "escalated": result.escalated, "tools": result.tools, "source": result.source,
           "latency_ms": {"llm": result.llm_ms},
           "llm": {"model": getattr(eng.llm, "last_model", None),
                   "rate_limit_waits": getattr(eng.llm, "rate_limit_waits", 0) - waits_before}}
    if body.speak:
        out |= await _speak(conn, eng, client, sess, result.reply, result.lang)
        if out.get("tts"):
            out["latency_ms"]["tts"] = out["tts"]["latency_ms"]
    return out


@router.post("/session/{session_id}/end")
def end(session_id: str, conn=Depends(get_conn), eng=Depends(engine)) -> dict:
    eng.end(conn, session_id)
    return {"ok": True}
