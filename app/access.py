"""Shared-passcode gate for the hosted demo.

When ACCESS_CODE is set, every /api call and every page except the passcode
page needs a cookie proving the visitor entered the code. This keeps strangers
from spending the Gnani credits and Gemini quota. With no ACCESS_CODE (local
development) the gate is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

COOKIE = "cc_access"
OPEN_PATHS = {"/access.html", "/api/access", "/api/health", "/css/app.css", "/favicon.ico"}
PAGES = {"/", "/index.html", "/admin.html"}

router = APIRouter(prefix="/api")


def _token(code: str) -> str:
    return hashlib.sha256(f"carecall-access:{code}".encode()).hexdigest()


def gate_enabled() -> bool:
    return bool(settings.access_code)


def has_access(request: Request) -> bool:
    if not gate_enabled():
        return True
    cookie = request.cookies.get(COOKIE, "")
    return secrets.compare_digest(cookie, _token(settings.access_code))


class AccessGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not gate_enabled() or path in OPEN_PATHS or has_access(request):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Access code required"}, status_code=401)
        if path in PAGES:
            return RedirectResponse(f"/access.html?next={quote(path)}", status_code=303)
        return await call_next(request)          # static JS/CSS hold no secrets


class AccessIn(BaseModel):
    code: str


@router.post("/access")
async def enter_code(body: AccessIn, request: Request, response: Response) -> dict:
    if not gate_enabled():
        return {"ok": True}
    if not secrets.compare_digest(body.code.strip(), settings.access_code):
        await asyncio.sleep(1.0)                 # slow down guessing
        raise HTTPException(401, "That access code is not correct.")
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, _token(settings.access_code), max_age=7 * 24 * 3600,
                        httponly=True, samesite="lax", secure=https)
    return {"ok": True}
