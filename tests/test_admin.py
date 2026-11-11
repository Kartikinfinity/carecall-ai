"""Admin API against a database seeded at the real current time (the dashboard is about 'today')."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import booking, db, main, routes_voice, seed
from app.config import Settings, now_ist
from app.dialog import DialogEngine
from app.gnani import GnaniClient


@pytest.fixture
def live_conn(tmp_path):
    c = db.connect(tmp_path / "admin.db")
    seed.seed(c)
    yield c
    c.close()


@pytest.fixture
def client(live_conn, tmp_path):
    def use_test_db():
        yield live_conn

    main.app.dependency_overrides[routes_voice.get_conn] = use_test_db
    main.app.dependency_overrides[main.get_conn] = use_test_db
    with TestClient(main.app) as c:
        main.app.state.gnani = GnaniClient(cfg=Settings(gnani_mock=True, gnani_api_key="x"), cache_dir=tmp_path)
        main.app.state.engine = DialogEngine(None)
        yield c
    main.app.dependency_overrides.clear()


def next_open_day(conn, doctor="DOC-101"):
    d = now_ist().date()
    for i in range(1, 8):
        day = (d + timedelta(days=i)).isoformat()
        if booking.available_slots(conn, doctor, day):
            return day
    raise AssertionError("no open day")


def test_summary(client):
    s = client.get("/api/admin/summary").json()
    assert s["date"] == now_ist().date().isoformat()
    assert s["open_followups"] == 1 and s["urgent_followups"] == 1     # seeded medical escalation
    assert s["open_slots_today"] >= 0 and s["appointments_today"] >= 0


def test_appointments_list_and_cancel(client):
    upcoming = client.get("/api/admin/appointments").json()
    assert upcoming and all(a["status"] != "CANCELLED" for a in upcoming)
    assert "patient" in upcoming[0] and upcoming[0]["demo_contact"].startswith("DEMO-IN-")
    with_cancelled = client.get("/api/admin/appointments", params={"include_cancelled": True}).json()
    assert any(a["status"] == "CANCELLED" for a in with_cancelled)

    target = next(a for a in upcoming if a["status"] == "CONFIRMED")
    assert client.post(f"/api/admin/appointments/{target['ref']}/cancel", json={"reason": "x"}).status_code == 422
    ok = client.post(f"/api/admin/appointments/{target['ref']}/cancel", json={"reason": "Doctor unavailable"})
    assert ok.json()["status"] == "CANCELLED"
    assert client.post(f"/api/admin/appointments/{target['ref']}/cancel",
                       json={"reason": "again please"}).status_code == 409
    assert client.post("/api/admin/appointments/CC-9999/cancel", json={"reason": "missing"}).status_code == 404


def test_followup_resolution(client):
    open_items = client.get("/api/admin/followups").json()
    assert len(open_items) == 1 and open_items[0]["priority"] == "URGENT"
    fid = open_items[0]["id"]
    assert client.post(f"/api/admin/followups/{fid}/resolve", json={"note": ""}).status_code == 422
    assert client.post(f"/api/admin/followups/{fid}/resolve", json={"note": "Called back, advised ER"}).status_code == 200
    assert client.post(f"/api/admin/followups/{fid}/resolve", json={"note": "again"}).status_code == 409
    assert client.get("/api/admin/followups").json() == []
    assert len(client.get("/api/admin/followups", params={"status": "ALL"}).json()) == 2
    assert client.get("/api/admin/summary").json()["open_followups"] == 0


def test_calls_and_transcript(client):
    calls = client.get("/api/admin/calls").json()
    assert len(calls) == 3 and all(c["turn_count"] >= 2 for c in calls)
    detail = client.get(f"/api/admin/calls/{calls[0]['id']}").json()
    assert detail["turns"][0]["role"] == "user"
    assert client.get("/api/admin/calls/99999").status_code == 404


def test_leave_toggle_blocks_slots(client, live_conn):
    day = next_open_day(live_conn)
    r = client.post("/api/admin/doctors/DOC-101/leave", json={"date": day, "on": True}).json()
    assert r["on_leave"] and booking.available_slots(live_conn, "DOC-101", day) == []
    doc = next(d for d in client.get("/api/admin/doctors").json() if d["id"] == "DOC-101")
    assert day in doc["leave"]
    client.post("/api/admin/doctors/DOC-101/leave", json={"date": day, "on": False})
    assert booking.available_slots(live_conn, "DOC-101", day)
    past = (now_ist().date() - timedelta(days=1)).isoformat()
    assert client.post("/api/admin/doctors/DOC-101/leave", json={"date": past, "on": True}).status_code == 422
    assert client.post("/api/admin/doctors/DOC-999/leave", json={"date": day, "on": True}).status_code == 404


def test_faq_edit_reaches_the_assistant(client, live_conn):
    new_en = "Free parking for 20 cars behind the clinic."
    r = client.put("/api/admin/faqs/FAQ-LOCATION", json={"answer_en": new_en, "answer_hi": "क्लिनिक के पीछे 20 गाड़ियों की मुफ़्त पार्किंग।"})
    assert r.status_code == 200
    eng = DialogEngine(None)
    sess, _ = eng.start(live_conn, "PAT-1001")
    assert new_en in eng._tool_clinic_info(live_conn, sess, None)["policies"]          # the assistant's source of policies
    assert client.put("/api/admin/faqs/NOPE", json={"answer_en": "hello world", "answer_hi": "नमस्ते दुनिया"}).status_code == 404


def test_reset_restores_demo_data(client, live_conn):
    client.post("/api/session", json={"patient_id": "PAT-1001", "speak": False})
    live_conn.execute("DELETE FROM followups")
    out = client.post("/api/admin/reset").json()
    assert out["ok"] and out["counts"]["followups"] == 2
    assert main.app.state.engine.sessions == {}
