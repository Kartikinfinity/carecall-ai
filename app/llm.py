"""Thin async wrapper around Gemini function calling.

The dialog engine forces a tool call on every step (mode=ANY) and ends a turn
when the model calls `say`, so replies always arrive as structured
{text, language} instead of free text that has to be parsed.

Quota handling (free tier): a per-day 429 cannot succeed on
retry, so that model is skipped for an hour and the fallback model is used; a
per-minute 429 with a short retryDelay is waited out once.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Protocol

from google import genai
from google.genai import types

from .config import settings

MAX_WAIT_FOR_RATE_LIMIT_S = 12.0
DAILY_QUOTA_BACKOFF_S = 3600.0


class LLMError(Exception):
    pass


class LLM(Protocol):
    async def step(self, system: str, contents: list[types.Content],
                   tools: list[types.FunctionDeclaration]) -> types.Content: ...


def _retry_delay(message: str) -> float | None:
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", message)
    return float(m.group(1)) if m else None


class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 fallback_model: str | None = None, thinking: str | None = None, retries: int = 1):
        key = api_key if api_key is not None else settings.gemini_api_key
        if not key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.model = model or settings.gemini_model
        fb = fallback_model if fallback_model is not None else os.getenv("GEMINI_FALLBACK_MODEL", "")
        self.models = [self.model] + ([fb] if fb and fb != self.model else [])
        self.thinking = (thinking or os.getenv("GEMINI_THINKING", "low")).upper()
        self.retries = retries
        self.last_model: str | None = None
        self.rate_limit_waits = 0          # cumulative; shown in the UI trace
        self._skip_until: dict[str, float] = {}
        self._client = genai.Client(api_key=key)

    async def step(self, system, contents, tools):
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[types.Tool(function_declarations=tools)],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking),
        )
        errors: list[str] = []
        for model in self.models:
            if self._skip_until.get(model, 0) > time.monotonic():
                errors.append(f"{model}: daily quota exhausted")
                continue
            for attempt in range(self.retries + 1):
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=model, contents=contents, config=config)
                except Exception as exc:          # network, 429, 5xx from the SDK
                    msg = str(exc)
                    errors.append(f"{model}: {msg[:160]}")
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        if "PerDay" in msg:
                            self._skip_until[model] = time.monotonic() + DAILY_QUOTA_BACKOFF_S
                            break                 # try the fallback model
                        delay = _retry_delay(msg)
                        if delay is not None and delay <= MAX_WAIT_FOR_RATE_LIMIT_S and attempt < self.retries:
                            self.rate_limit_waits += 1
                            await asyncio.sleep(delay + 0.5)
                            continue
                        break
                    if attempt < self.retries:
                        await asyncio.sleep(1.5)
                    continue
                cand = resp.candidates[0] if resp.candidates else None
                if cand and cand.content and cand.content.parts:
                    self.last_model = model
                    return cand.content
                errors.append(f"{model}: empty response ({cand.finish_reason if cand else None})")
        raise LLMError("Gemini unavailable: " + " | ".join(errors[-3:]))
