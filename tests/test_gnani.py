import asyncio
import json
import struct

import httpx
import pytest

from app.config import Settings
from app.gnani import (GnaniClient, GnaniError, fix_wav_header, keyterms_from_db,
                       sanitize_keyterms, tone_wav, wav_seconds)


def cfg(**kw):
    base = dict(gnani_api_key="test-key", gnani_mock=False, daily_gnani_cap=0,
                gnani_stt_model="prisma-v3.0", gnani_tts_model="timbre-v2.5")
    base.update(kw)
    return Settings(**base)


async def _no_sleep(_):
    return None


def make_client(handler, tmp_path, **kw):
    return GnaniClient(cfg=cfg(**kw), transport=httpx.MockTransport(handler),
                       cache_dir=tmp_path / "cache", sleep=_no_sleep)


def streamed_wav() -> bytes:
    """A WAV like Timbre returns: sizes set to 0xFFFFFFFF."""
    good = tone_wav(0.5)
    bad = bytearray(good)
    struct.pack_into("<I", bad, 4, 0xFFFFFFFF)
    struct.pack_into("<I", bad, 40, 0xFFFFFFFF)
    return bytes(bad)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ WAV helpers

def test_fix_wav_header_restores_sizes():
    fixed = fix_wav_header(streamed_wav())
    assert struct.unpack_from("<I", fixed, 4)[0] == len(fixed) - 8
    assert struct.unpack_from("<I", fixed, 40)[0] == len(fixed) - 44
    assert abs(wav_seconds(fixed) - 0.5) < 0.01


def test_fix_wav_header_ignores_non_wav():
    assert fix_wav_header(b"not audio") == b"not audio"


def test_sanitize_keyterms():
    terms = sanitize_keyterms(["Chandrakar", "chandrakar", " ", "<|bad|>", *[f"t{i}" for i in range(300)]])
    assert terms[:2] == ["Chandrakar", "bad"] and len(terms) == 200


def test_keyterms_from_db_uses_surnames(conn):
    terms = keyterms_from_db(conn)
    assert {"Chandrakar", "Baghel", "Deshmukh", "Sahu"} <= set(terms)
    assert "Dr. Aarav Chandrakar" not in terms


# ------------------------------------------------------------------ STT

def test_transcribe_sends_documented_fields(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True, "request_id": "gnani-123",
                                         "transcript": " कल सुबह 10:30 बजे ", "detected_language_code": "hi-IN"})

    client = make_client(handler, tmp_path)
    res = run(client.transcribe(tone_wav(1.0), keyterms=["Chandrakar"], previous_text="Namaste"))
    assert seen["url"] == "https://api.vachana.ai/stt/v3"
    assert seen["headers"]["X-API-Key-ID"] == "test-key" and seen["headers"]["X-API-Request-ID"]
    body = seen["body"].decode("utf-8", "replace")
    for part in ('name="model"', "prisma-v3.0", 'name="language_code"', "hi-IN,en-IN",
                 'name="format"', "transcribe", 'name="keyterms"', '["Chandrakar"]',
                 'name="previous_text"', 'name="audio_file"; filename="turn.wav"'):
        assert part in body
    assert res.text == "कल सुबह 10:30 बजे" and res.language == "hi-IN" and res.request_id == "gnani-123"


def test_transcribe_skips_api_for_tiny_audio_and_rejects_long_audio(tmp_path):
    def handler(_):
        raise AssertionError("API must not be called")

    client = make_client(handler, tmp_path)
    assert run(client.transcribe(tone_wav(0.05))).text == ""
    with pytest.raises(GnaniError) as e:
        run(client.transcribe(tone_wav(31)))
    assert e.value.kind == "audio"
    with pytest.raises(GnaniError):
        run(client.transcribe(b"garbage"))


def test_retries_429_then_succeeds(tmp_path):
    calls = []

    def handler(_):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"detail": {"error_code": "RATE_LIMITED",
                                                        "message": "Only 1 active request is allowed at a time."}})
        return httpx.Response(200, json={"success": True, "transcript": "ok"})

    res = run(make_client(handler, tmp_path).transcribe(tone_wav(1.0)))
    assert res.text == "ok" and len(calls) == 3


def test_gives_up_after_retries(tmp_path):
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(503, json={"success": False, "error": {"type": "SERVICE_UNAVAILABLE", "message": "x"}})

    with pytest.raises(GnaniError) as e:
        run(make_client(handler, tmp_path).transcribe(tone_wav(1.0)))
    assert len(calls) == 4 and e.value.kind == "server"


@pytest.mark.parametrize("status,kind", [(400, "bad_request"), (401, "auth"), (402, "credits"),
                                         (403, "scope"), (422, "bad_request")])
def test_client_errors_are_not_retried(tmp_path, status, kind):
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(status, json={"detail": {"error_code": "X", "message": "nope"}})

    with pytest.raises(GnaniError) as e:
        run(make_client(handler, tmp_path).transcribe(tone_wav(1.0)))
    assert len(calls) == 1 and e.value.kind == kind and e.value.user_message


def test_network_errors_are_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"success": True, "transcript": "back"})

    assert run(make_client(handler, tmp_path).transcribe(tone_wav(1.0))).text == "back"


def test_requests_are_serialized(tmp_path):
    """Never more than one request in flight, even when callers overlap."""
    active, peak = 0, 0

    async def handler(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"success": True, "transcript": "x"})

    async def go():
        client = make_client(handler, tmp_path)
        await asyncio.gather(*[client.transcribe(tone_wav(0.5)) for _ in range(5)])

    run(go())
    assert peak == 1


def test_daily_cap(tmp_path):
    def handler(_):
        return httpx.Response(200, json={"success": True, "transcript": "x"})

    async def go():
        client = make_client(handler, tmp_path, daily_gnani_cap=2)
        await client.transcribe(tone_wav(0.5))
        await client.transcribe(tone_wav(0.5))
        with pytest.raises(GnaniError) as e:
            await client.transcribe(tone_wav(0.5))
        assert e.value.kind == "cap" and client.calls_today() == 2

    run(go())


def test_missing_key(tmp_path):
    with pytest.raises(GnaniError) as e:
        run(make_client(lambda r: None, tmp_path, gnani_api_key="").transcribe(tone_wav(1.0)))
    assert e.value.kind == "auth"


# ------------------------------------------------------------------ TTS

def test_synthesize_body_voice_and_header_fix(tmp_path):
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(200, content=streamed_wav(), headers={"content-type": "audio/wav"})

    res = run(make_client(handler, tmp_path).synthesize("जी हाँ, Doctor Chandrakar available हैं।", "hi-en"))
    assert seen["url"] == "https://api.vachana.ai/api/v1/tts/inference"
    body = seen["json"]
    assert body["model"] == "timbre-v2.5" and body["voice"] == "Poorvi" and body["language"] == "hi-en"
    assert body["audio_config"] == {"sample_rate": 24000, "num_channels": 1, "sample_width": 2,
                                    "encoding": "linear_pcm", "container": "wav"}
    assert struct.unpack_from("<I", res.audio, 4)[0] == len(res.audio) - 8
    assert res.voice == "Poorvi" and not res.cached


@pytest.mark.parametrize("lang,voice", [("hi-IN", "Nalini"), ("en-IN", "Kaveri"), ("hi-en", "Poorvi"),
                                        ("xx", "Poorvi")])
def test_voice_selection(tmp_path, lang, voice):
    assert make_client(lambda r: None, tmp_path).voice_for(lang)[0] == voice


def test_v3_uses_one_multilingual_voice(tmp_path):
    client = make_client(lambda r: None, tmp_path, gnani_tts_model="timbre-v3.0")
    assert client.voice_for("en-IN") == ("Shivani", "auto")


def test_tts_cache_avoids_second_call(tmp_path):
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(200, content=streamed_wav())

    async def go():
        client = make_client(handler, tmp_path)
        first = await client.synthesize("Namaste", "hi-en")
        second = await client.synthesize("Namaste", "hi-en")
        return first, second

    first, second = run(go())
    assert len(calls) == 1 and second.cached and second.audio == first.audio


def test_tts_rejects_non_wav_and_empty(tmp_path):
    client = make_client(lambda r: httpx.Response(200, content=b"<html>"), tmp_path)
    with pytest.raises(GnaniError):
        run(client.synthesize("hello", "en-IN"))
    with pytest.raises(GnaniError):
        run(client.synthesize("   ", "en-IN"))


def test_mock_mode_makes_no_requests(tmp_path):
    def handler(_):
        raise AssertionError("no network in mock mode")

    client = make_client(handler, tmp_path, gnani_mock=True)
    stt = run(client.transcribe(tone_wav(1.0)))
    tts = run(client.synthesize("hello", "en-IN"))
    assert stt.text and stt.request_id.startswith("mock-")
    assert wav_seconds(tts.audio) > 0
