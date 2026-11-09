import pytest
from fastapi.testclient import TestClient

from app import access, main
from app.config import Settings


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(access, "settings", Settings(access_code="open-sesame"))
    with TestClient(main.app) as c:
        yield c


def test_api_and_pages_locked_without_code(gated):
    assert gated.get("/api/patients").status_code == 401
    assert gated.post("/api/admin/reset").status_code == 401
    page = gated.get("/admin.html", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/access.html?next=/admin.html"
    assert gated.get("/access.html").status_code == 200
    assert gated.get("/api/health").status_code == 200


def test_wrong_code_rejected_right_code_unlocks(gated):
    assert gated.post("/api/access", json={"code": "guess"}).status_code == 401
    ok = gated.post("/api/access", json={"code": " open-sesame "})
    assert ok.status_code == 200 and "httponly" in ok.headers["set-cookie"].lower()
    assert gated.get("/api/patients").status_code == 200
    assert gated.get("/", follow_redirects=False).status_code == 200


def test_gate_off_when_no_code(monkeypatch):
    monkeypatch.setattr(access, "settings", Settings(access_code=""))
    with TestClient(main.app) as c:
        assert c.get("/api/patients").status_code == 200
