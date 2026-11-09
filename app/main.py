"""FastAPI entry point. Run: uvicorn app.main:app --reload"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI

from . import db, seed
from .config import settings


def ensure_database() -> None:
    """Seed on first run, or on every boot when RESEED_ON_BOOT=1 (hosted demo)."""
    conn = db.connect(settings.database_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'appointments'").fetchone()
        if not exists or os.getenv("RESEED_ON_BOOT") == "1":
            seed.seed(conn)
    finally:
        conn.close()


def make_llm():
    from .llm import GeminiLLM, LLMError
    try:
        return GeminiLLM()
    except LLMError:
        return None          # turns then answer with a polite "staff will call you" fallback


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .dialog import DialogEngine
    from .gnani import GnaniClient
    ensure_database()
    app.state.engine = DialogEngine(make_llm())
    app.state.gnani = GnaniClient()
    yield
    await app.state.gnani.aclose()


app = FastAPI(title="CareCall AI", lifespan=lifespan)


def get_conn() -> Iterator:
    """One SQLite connection per request (connections are not shared across threads)."""
    conn = db.connect(settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/api/health")
def health(conn=Depends(get_conn)) -> dict:
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("doctors", "patients", "appointments", "faqs")}
    return {"ok": True, "counts": counts, "stt_model": settings.gnani_stt_model,
            "tts_model": settings.gnani_tts_model, "llm_model": settings.gemini_model,
            "gnani_mock": settings.gnani_mock}


from fastapi.staticfiles import StaticFiles  # noqa: E402

from .config import ROOT  # noqa: E402
from .routes_admin import router as admin_router  # noqa: E402
from .access import AccessGate  # noqa: E402
from .access import router as access_router  # noqa: E402
from .routes_voice import router as voice_router  # noqa: E402

app.add_middleware(AccessGate)
app.include_router(access_router)
app.include_router(voice_router)
app.include_router(admin_router)

# Static UI last, so /api routes win. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
