"""Settings loaded from the environment (.env in development)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# India has no daylight saving, so a fixed offset is exact and avoids needing
# the tzdata package on Windows.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    return datetime.now(IST)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "carecall.db")))
    gnani_api_key: str = os.getenv("GNANI_API_KEY", "").strip()
    gnani_base_url: str = os.getenv("GNANI_BASE_URL", "https://api.vachana.ai").rstrip("/")
    gnani_stt_model: str = os.getenv("GNANI_STT_MODEL", "prisma-v3.0")
    gnani_stt_langs: str = os.getenv("GNANI_STT_LANGS", "hi-IN,en-IN")
    gnani_tts_model: str = os.getenv("GNANI_TTS_MODEL", "timbre-v2.5")
    gnani_mock: bool = os.getenv("GNANI_MOCK", "0") == "1"
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    access_code: str = os.getenv("ACCESS_CODE", "").strip()
    daily_gnani_cap: int = _int("DAILY_GNANI_CAP", 300)


settings = Settings()
