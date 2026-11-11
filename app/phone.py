"""Phone line: connects a telephony provider's audio stream to the live conversation.

Two provider dialects are understood, chosen from the first message on the socket:
- Twilio Media Streams ("streamSid", media / mark / clear events);
- Plivo-style audio streams, which Vobiz also uses ("streamId", playAudio / clearAudio).

The line is off unless PHONE_TOKEN is set. The provider must present that token,
so nobody else can open a stream and spend speech credits. The caller's number is
never read from the provider's messages, so it cannot be stored or shown.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from . import db
from .config import settings
from .realtime import PhoneCall, open_stt

log = logging.getLogger("carecall.phone")
router = APIRouter(prefix="/phone")
CHUNK = 4000          # half a second of mu-law per message


def _token_ok(token: str | None) -> bool:
    return bool(settings.phone_token) and secrets.compare_digest(token or "", settings.phone_token)


def _stream_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"wss://{host}/phone/stream"


# ------------------------------------------------------------------ call webhooks (what to do when the phone rings)

@router.post("/voice/{token}")
@router.get("/voice/{token}")
def twilio_voice(token: str, request: Request) -> Response:
    """Twilio asks this when a call arrives; the answer tells it to stream the audio to us."""
    if not _token_ok(token):
        raise HTTPException(404)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
           f'<Stream url={quoteattr(_stream_url(request))}><Parameter name="token" value={quoteattr(token)}/></Stream>'
           '</Connect></Response>')
    return Response(xml, media_type="application/xml")


@router.post("/plivo/{token}")
@router.get("/plivo/{token}")
def plivo_voice(token: str, request: Request) -> Response:
    """The same for Plivo-style providers (Vobiz), which put the token in the stream URL."""
    if not _token_ok(token):
        raise HTTPException(404)
    url = f"{_stream_url(request)}?token={token}"
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
           '<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">'
           f'{url}</Stream></Response>')
    return Response(xml, media_type="application/xml")


# ------------------------------------------------------------------ provider dialects

class _Line:
    """Sends audio and commands to the caller in one provider's message format."""

    def __init__(self, ws: WebSocket, stream_id: str, call_id: str | None):
        self.ws, self.stream_id, self.call_id = ws, stream_id, call_id

    async def _send(self, message: dict) -> None:
        await self.ws.send_text(json.dumps(message))

    async def transfer(self) -> bool:
        return False          # bringing a human onto the line is added per provider


class TwilioLine(_Line):
    async def play(self, ulaw: bytes) -> None:
        for i in range(0, len(ulaw), CHUNK):
            await self._send({"event": "media", "streamSid": self.stream_id,
                              "media": {"payload": base64.b64encode(ulaw[i:i + CHUNK]).decode("ascii")}})
        await self._send({"event": "mark", "streamSid": self.stream_id, "mark": {"name": "said"}})

    async def clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_id})


class PlivoLine(_Line):
    async def play(self, ulaw: bytes) -> None:
        for i in range(0, len(ulaw), CHUNK):
            await self._send({"event": "playAudio", "media": {
                "contentType": "audio/x-mulaw", "sampleRate": 8000,
                "payload": base64.b64encode(ulaw[i:i + CHUNK]).decode("ascii")}})

    async def clear(self) -> None:
        await self._send({"event": "clearAudio", "streamId": self.stream_id})


# ------------------------------------------------------------------ the audio stream

@router.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    if not settings.phone_token:
        await ws.close(code=1008)
        return
    await ws.accept()
    line = None
    try:
        # Wait for the provider's "start" message; it says which dialect this is.
        while line is None:
            msg = json.loads(await ws.receive_text())
            if msg.get("event") != "start":
                continue
            start = msg.get("start") or {}
            if "streamSid" in msg or "streamSid" in start:
                token = (start.get("customParameters") or {}).get("token")
                line = TwilioLine(ws, msg.get("streamSid") or start.get("streamSid"), start.get("callSid"))
            else:
                token = ws.query_params.get("token")
                line = PlivoLine(ws, start.get("streamId") or msg.get("streamId") or "", start.get("callId"))
            if not _token_ok(token):
                await ws.close(code=1008)
                return

        async def inbound():
            while True:
                msg = json.loads(await ws.receive_text())
                event = msg.get("event")
                if event == "media":
                    track = (msg.get("media") or {}).get("track", "inbound")
                    if track == "inbound":
                        yield base64.b64decode(msg["media"]["payload"])
                elif event in ("stop", "closed"):
                    return

        state = ws.app.state
        conn = getattr(state, "phone_conn_factory", lambda: db.connect(settings.database_path))()
        try:
            call = PhoneCall(state.engine, state.gnani, conn, line,
                             stt_factory=getattr(state, "stt_factory", open_stt), lang=settings.phone_lang)
            await call.run(inbound())
        finally:
            if not hasattr(state, "phone_conn_factory"):
                conn.close()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("phone stream failed")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
