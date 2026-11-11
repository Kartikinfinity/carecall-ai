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
    """The LLMs to use, fastest first (LLM_ORDER); each falls through to the next when it is rate-limited."""
    from .llm import FallbackLLM, GeminiLLM, LLMError, OpenAICompatLLM
    available = {}
    try:
        available["gemini"] = [GeminiLLM()]
    except LLMError:
        pass
    if settings.groq_api_key:
        available["groq"] = [OpenAICompatLLM(settings.groq_api_key, model.strip(), settings.groq_base_url, name="groq")
                             for model in settings.groq_model.split(",") if model.strip()]
    order = [name.strip() for name in settings.llm_order.split(",") if name.strip() in available]
    order += [name for name in available if name not in order]
    backends = [backend for name in order for backend in available[name]]
    if not backends:
        return None          # turns then answer with a polite "staff will call you" fallback
    return backends[0] if len(backends) == 1 else FallbackLLM(*backends)


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


@app.middleware("http")
async def revalidate_pages(request, call_next):
    """Ask browsers to check for a newer page or script on each load (a cheap 304),
    so a fresh deploy is never hidden behind a stale cached file."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


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
from .phone import router as phone_router  # noqa: E402

app.add_middleware(AccessGate)
app.include_router(access_router)
app.include_router(voice_router)
app.include_router(admin_router)
app.include_router(phone_router)

# Static UI last, so /api routes win. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
