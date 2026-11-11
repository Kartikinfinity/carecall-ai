"""Phone line: audio conversion, the live call loop, and the provider WebSocket.

Gnani is mocked and the speech-to-text stream is scripted, so no credits are used.
"""
import asyncio
import base64
import json
import math
import dataclasses
import struct

import pytest
from fastapi.testclient import TestClient

from app import main, phone as phone_mod, phrases, routes_voice
from app.audio import pcm16_to_ulaw, ulaw_seconds, ulaw_to_pcm16
from app.config import Settings, settings
from app.dialog import DialogEngine
from app.gnani import GnaniClient
from app.realtime import PhoneCall, SttEvent, SttStream

from .conftest import MONDAY_8AM
from .test_dialog import TUE, FakeLLM, ai_bookings, run, say
from .test_gnani import make_client

TOKEN = "test-phone-token"
CALLER_NUMBER = "+919876543210"          # must never reach the database
BOOKING_STEPS = ([("find_slots", {"doctor": "Dr. Chandrakar", "date": TUE, "time": "10:30"})], [say("2 slots")],
                 [("propose_booking", {"doctor": "Chandrakar", "date": TUE, "time": "10:30", "patient_name": "Asha Rao"})],
                 [say("confirm?")],
                 [("confirm_pending", {})], [say("booked")])


# ------------------------------------------------------------------ audio

def test_ulaw_round_trip_keeps_speech_shape():
    pcm = struct.pack("<800h", *[int(12000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(800)])
    ulaw = pcm16_to_ulaw(pcm)
    back = struct.unpack("<800h", ulaw_to_pcm16(ulaw))
    original = struct.unpack("<800h", pcm)
    assert len(ulaw) == 800 and ulaw_seconds(ulaw) == 0.1
    assert max(abs(a - b) for a, b in zip(original, back)) < 600          # mu-law is lossy, but only slightly
    assert ulaw_to_pcm16(b"\xff") == b"\x00\x00" and pcm16_to_ulaw(b"\x00\x00") == b"\xff"


def test_ulaw_handles_the_loudest_samples():
    loud = struct.pack("<2h", 32767, -32768)
    a, b = struct.unpack("<2h", ulaw_to_pcm16(pcm16_to_ulaw(loud)))
    assert a > 30000 and b < -30000


# ------------------------------------------------------------------ Gnani requests for a phone line

def test_phone_speech_is_requested_as_8k_mulaw_and_cached(tmp_path):
    seen = []

    def handler(request):
        import httpx
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"\xff" * 800, headers={"content-type": "audio/basic"})

    client = make_client(handler, tmp_path)
    first = run(client.synthesize("namaste", "hi-en", audio_format="mulaw"))
    again = run(client.synthesize("namaste", "hi-en", audio_format="mulaw"))
    assert seen[0]["audio_config"] == {"sample_rate": 8000, "num_channels": 1, "sample_width": 2, "container": "mulaw"}
    assert first.audio == b"\xff" * 800 and again.cached and len(seen) == 1


def test_stream_headers_carry_the_calls_languages():
    cfg = Settings(gnani_api_key="k", gnani_stt_model="prisma-v3.0")
    hinglish = SttStream(cfg, "hi-IN,en-IN").headers()
    assert hinglish["language_code"] == "hi-IN" and hinglish["multi_lang_codes"] == "hi-IN,en-IN"
    assert hinglish["x-sample-rate"] == "8000" and hinglish["format"] == "transcribe"
    auto = SttStream(cfg, "indic-auto").headers()
    assert auto["language_code"] == "indic-auto" and "multi_lang_codes" not in auto


class FakeSocket:
    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)


def test_stream_sends_exact_1024_byte_frames():
    stream = SttStream(Settings(gnani_api_key="k"), "hi-IN")
    stream._ws = FakeSocket()
    run(stream.send_pcm(b"\x01" * 700))
    assert stream._ws.frames == []
    run(stream.send_pcm(b"\x01" * 1500))
    assert [len(f) for f in stream._ws.frames] == [1024, 1024]


# ------------------------------------------------------------------ the call loop

class FakeStt:
    """Plays back scripted events; counts the audio it was given."""

    def __init__(self, events, gap=0.0):
        self.script, self.gap = list(events), gap
        self.pcm_bytes = 0
        self.closed = False

    async def send_pcm(self, pcm):
        self.pcm_bytes += len(pcm)

    async def events(self):
        for event in self.script:
            await asyncio.sleep(self.gap)
            yield event

    async def close(self):
        self.closed = True


class FakeLine:
    def __init__(self, transfer_ok=False):
        self.log, self.transfer_ok = [], transfer_ok

    async def play(self, ulaw):
        self.log.append(("play", len(ulaw)))

    async def clear(self):
        self.log.append(("clear", 0))

    async def transfer(self):
        self.log.append(("transfer", 0))
        return self.transfer_ok


def heard(text, lang="hi-IN"):
    return SttEvent("transcript", text, lang, "seg-1", 110)


async def no_audio():
    return
    yield b""


def phone_call(conn, tmp_path, events, steps=(), line=None, **kw):
    engine = DialogEngine(FakeLLM(*steps), now_fn=lambda: MONDAY_8AM)
    gnani = GnaniClient(cfg=Settings(gnani_mock=True, gnani_api_key="x"), cache_dir=tmp_path / "cache")
    stt = FakeStt(events)
    line = line or FakeLine()

    async def factory(cfg, codes):
        stt.codes = codes
        return stt

    call = PhoneCall(engine, gnani, conn, line, stt_factory=factory, **kw)
    return call, stt, line


def test_a_booking_over_the_phone(conn, tmp_path):
    events = [heard("Kal Dr. Chandrakar ka 10:30 ka slot?"), heard("10:30 wala, mera naam Asha Rao hai"),
              heard("Haan, book kar do")]
    call, stt, line = phone_call(conn, tmp_path, events, BOOKING_STEPS)
    run(call.run(no_audio()))
    rows = ai_bookings(conn)
    assert len(rows) == 1 and (rows[0]["date"], rows[0]["start_time"]) == (TUE, "10:30")
    assert [kind for kind, _ in line.log] == ["play"] * 4          # greeting + three replies
    record = conn.execute("SELECT * FROM calls WHERE id = ?", (call.sess.call_id,)).fetchone()
    assert record["channel"] == "PHONE" and record["outcome"] == "BOOKED" and record["ended_at"]
    assert stt.codes == "indic-auto" and stt.closed          # a phone call has no language picker


def test_phone_call_in_a_fixed_language_greets_in_it(conn, tmp_path):
    call, stt, line = phone_call(conn, tmp_path, [], lang="ta-IN")
    run(call.run(no_audio()))
    assert stt.codes == "ta-IN,en-IN" and call.sess.lang == "ta-IN"
    first = conn.execute("SELECT text FROM turns WHERE call_id = ? ORDER BY id LIMIT 1", (call.sess.call_id,)).fetchone()
    assert first["text"] == phrases.reply("GREETING", "ta-IN")


def test_talking_over_the_receptionist_stops_her(conn, tmp_path):
    call, stt, line = phone_call(conn, tmp_path, [SttEvent("speech_start")])
    run(call.run(no_audio()))
    assert line.log == [("play", 1600), ("clear", 0)]          # greeting was still playing when the caller spoke


def test_speech_start_in_silence_clears_nothing(conn, tmp_path):
    call, stt, line = phone_call(conn, tmp_path, [SttEvent("speech_start")])
    stt.gap = 0.35                                             # the 0.2 s mock greeting has finished by then
    run(call.run(no_audio()))
    assert line.log == [("play", 1600)]


def test_emergency_on_the_phone_gets_fixed_reply_and_a_transfer_attempt(conn, tmp_path):
    call, stt, line = phone_call(conn, tmp_path, [heard("mere seene mein bahut dard ho raha hai"), heard("hello?")],
                                 line=FakeLine(transfer_ok=True))
    run(call.run(no_audio()))
    assert [kind for kind, _ in line.log] == ["play", "play", "transfer"]          # stops listening once transferred
    ticket = conn.execute("SELECT priority FROM followups WHERE call_id = ?", (call.sess.call_id,)).fetchone()
    assert ticket["priority"] == "URGENT" and call.engine.llm.calls == 0          # the LLM never saw it


def test_a_slow_answer_gets_a_one_moment_filler(conn, tmp_path):
    class SlowLLM(FakeLLM):
        async def step(self, *a):
            await asyncio.sleep(0.15)
            return await super().step(*a)

    call, stt, line = phone_call(conn, tmp_path, [heard("clinic kab khulta hai")], hold_after=0.05)
    call.engine.llm = SlowLLM([say("9 baje")])
    run(call.run(no_audio()))
    assert [kind for kind, _ in line.log] == ["play", "play", "play"]          # greeting, "one moment", the answer


def test_empty_transcripts_and_stream_errors_do_not_crash_the_call(conn, tmp_path):
    call, stt, line = phone_call(conn, tmp_path, [heard(""), SttEvent("error", "socket closed"), heard("ignored")])
    run(call.run(no_audio()))
    assert call.turns == 0 and [kind for kind, _ in line.log] == ["play"]


def test_caller_audio_is_converted_and_forwarded(conn, tmp_path):
    async def audio():
        for _ in range(5):
            yield b"\xff" * 160          # five 20 ms phone packets

    call, stt, line = phone_call(conn, tmp_path, [])
    stt.gap = 0.05
    stt.script = [SttEvent("speech_start")]          # keep the call open long enough to pump audio
    call.speaking = lambda: False
    run(call.run(audio()))
    assert stt.pcm_bytes == 5 * 160 * 2


# ------------------------------------------------------------------ the provider WebSocket

@pytest.fixture
def phone(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(phone_mod, "settings", dataclasses.replace(settings, phone_token=TOKEN, phone_lang="auto"))

    def use_test_db():
        yield conn

    main.app.dependency_overrides[routes_voice.get_conn] = use_test_db
    main.app.dependency_overrides[main.get_conn] = use_test_db
    with TestClient(main.app) as client:
        state = main.app.state
        state.gnani = GnaniClient(cfg=Settings(gnani_mock=True, gnani_api_key="x"), cache_dir=tmp_path / "cache")
        state.engine = DialogEngine(FakeLLM(*BOOKING_STEPS), now_fn=lambda: MONDAY_8AM)
        state.phone_conn_factory = lambda: conn
        state.fake_stt = FakeStt([heard("Kal Dr. Chandrakar ka 10:30 ka slot?"), heard("10:30 wala, mera naam Asha Rao hai"),
                                  heard("Haan, book kar do")], gap=0.02)

        async def factory(cfg, codes):
            return state.fake_stt

        state.stt_factory = factory
        yield client
        del state.phone_conn_factory, state.stt_factory, state.fake_stt
    main.app.dependency_overrides.clear()


def read_until_closed(ws):
    messages = []
    try:
        while True:
            messages.append(json.loads(ws.receive_text()))
    except Exception:  # noqa: BLE001  (the server closes the socket when the call ends)
        pass
    return messages


def test_twilio_webhook_points_the_call_at_our_stream(phone):
    xml = phone.post(f"/phone/voice/{TOKEN}", headers={"host": "clinic.example"}).text
    assert '<Stream url="wss://clinic.example/phone/stream">' in xml and f'value="{TOKEN}"' in xml
    assert phone.post("/phone/voice/wrong").status_code == 404


def test_a_twilio_call_books_an_appointment_and_stores_no_number(phone, conn):
    with phone.websocket_connect("/phone/stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call"}))
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ123", "start": {
            "streamSid": "MZ123", "callSid": "CA1", "customParameters": {"token": TOKEN, "from": CALLER_NUMBER}}}))
        for _ in range(10):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ123", "media": {
                "track": "inbound", "payload": base64.b64encode(b"\xff" * 160).decode()}}))
        messages = read_until_closed(ws)
    media = [m for m in messages if m["event"] == "media"]
    assert len(media) == 4 and all(m["streamSid"] == "MZ123" for m in media)          # greeting + three replies
    assert base64.b64decode(media[0]["media"]["payload"]) == b"\xff" * 1600
    assert sum(m["event"] == "mark" for m in messages) == 4
    assert len(ai_bookings(conn)) == 1
    dump = "\n".join(str(tuple(row)) for table in ("calls", "turns", "followups", "patients", "appointments")
                     for row in conn.execute(f"SELECT * FROM {table}"))
    assert "9876543210" not in dump and "CA1" not in dump


def test_a_plivo_style_call_uses_play_audio_messages(phone):
    main.app.state.fake_stt.script = []
    with phone.websocket_connect(f"/phone/stream?token={TOKEN}") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamId": "s-1", "callId": "c-1"}}))
        messages = read_until_closed(ws)
    assert [m["event"] for m in messages] == ["playAudio"]
    assert messages[0]["media"]["contentType"] == "audio/x-mulaw" and messages[0]["media"]["sampleRate"] == 8000


def test_stream_refuses_a_wrong_token(phone, conn):
    before = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    with phone.websocket_connect("/phone/stream") as ws:
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "streamSid": "MZ1", "customParameters": {"token": "nope"}}}))
        assert read_until_closed(ws) == []
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == before


def test_phone_line_is_off_without_a_token(phone, monkeypatch):
    monkeypatch.setattr(phone_mod, "settings", dataclasses.replace(settings, phone_token=""))
    assert phone.post(f"/phone/voice/{TOKEN}").status_code == 404
    with pytest.raises(Exception):
        with phone.websocket_connect("/phone/stream") as ws:
            ws.receive_text()
