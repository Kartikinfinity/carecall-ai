"""HTTP API with mock Gnani, a scripted LLM and a temporary database."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import main, routes_voice
from app.config import Settings
from app.dialog import DialogEngine
from app.gnani import GnaniClient, tone_wav

from .conftest import MONDAY_8AM
from .test_dialog import FakeLLM, say


@pytest.fixture
def client(conn, tmp_path):
    def use_test_db():
        yield conn

    main.app.dependency_overrides[routes_voice.get_conn] = use_test_db
    main.app.dependency_overrides[main.get_conn] = use_test_db
    with TestClient(main.app) as c:
        main.app.state.gnani = GnaniClient(cfg=Settings(gnani_mock=True, gnani_api_key="x"),
                                           cache_dir=tmp_path / "cache")
        main.app.state.engine = DialogEngine(
            FakeLLM([("find_slots", {"doctor": "Chandrakar"})], [say("Do slots hain.")]),
            now_fn=lambda: MONDAY_8AM)
        yield c
    main.app.dependency_overrides.clear()


def test_spoken_turn_flow(client):
    start = client.post("/api/session", json={"patient_id": "PAT-1004"}).json()
    assert start["patient_name"] == "Kavita Dewangan" and base64.b64decode(start["audio_b64"])[:4] == b"RIFF"

    stt = client.post("/api/transcribe", data={"session_id": start["session_id"]},
                      files={"audio": ("turn.wav", tone_wav(1.0), "audio/wav")}).json()
    assert stt["text"] and stt["request_id"].startswith("mock-") and stt["model"] == "prisma-v3.0"

    turn = client.post("/api/turn", json={"session_id": start["session_id"], "text": stt["text"], "stt": stt}).json()
    assert turn["reply"] == "Do slots hain." and turn["card"]["type"] == "slots"
    assert turn["tts"]["voice"] in ("Poorvi", "Nalini", "Kaveri") and turn["audio_b64"]


def test_bad_audio_and_unknown_session(client):
    start = client.post("/api/session", json={"speak": False}).json()
    bad = client.post("/api/transcribe", data={"session_id": start["session_id"]},
                      files={"audio": ("x.wav", b"not a wav", "audio/wav")})
    assert bad.status_code == 422
    assert client.post("/api/turn", json={"session_id": "missing", "text": "hi"}).status_code == 404
    assert client.post("/api/transcribe", data={"session_id": "missing"},
                       files={"audio": ("x.wav", tone_wav(1.0), "audio/wav")}).status_code == 404


def test_static_pages_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Call the clinic" in page.text
    assert client.get("/js/recorder.js").status_code == 200
    assert client.get("/api/health").json()["ok"]
