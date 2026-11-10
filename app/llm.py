"""LLM backends for the dialog engine.

The dialog engine forces a tool call on every step and ends a turn when the
model calls `say`, so replies always arrive as structured {text, language}
instead of free text that has to be parsed.

- GeminiLLM: the main model (Gemini function calling).
- OpenAICompatLLM: any OpenAI-compatible chat API with tool calling; used with
  Groq's free tier as a backup when Gemini is rate-limited.
- FallbackLLM: tries each backend in order.

Gemini free-tier quota handling: a per-day 429 cannot succeed on retry, so that
model is skipped for an hour; a per-minute 429 with a short retryDelay is waited
out once.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from typing import Protocol

import httpx

from google import genai
from google.genai import types

from .config import settings

MAX_WAIT_FOR_RATE_LIMIT_S = 12.0
DAILY_QUOTA_BACKOFF_S = 3600.0


class LLMError(Exception):
    def __init__(self, message: str, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


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
        rate_limited = False
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
                        rate_limited = True
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
        raise LLMError("Gemini unavailable: " + " | ".join(errors[-3:]), rate_limited=rate_limited)


# --------------------------------------------------------------------------- OpenAI-compatible

def to_chat_messages(system: str, contents: list[types.Content]) -> list[dict]:
    """Convert genai-style history (text / function_call / function_response parts) to chat messages."""
    messages: list[dict] = [{"role": "system", "content": system}]
    counter = itertools.count(1)
    pending_ids: list[str] = []          # tool calls still waiting for their responses, in order
    for content in contents:
        parts = content.parts or []
        if content.role == "model":
            text = " ".join(p.text for p in parts if p.text).strip()
            calls = []
            for p in parts:
                if p.function_call:
                    call_id = p.function_call.id or f"call_{next(counter)}"
                    pending_ids.append(call_id)
                    calls.append({"id": call_id, "type": "function", "function": {
                        "name": p.function_call.name,
                        "arguments": json.dumps(dict(p.function_call.args or {}), ensure_ascii=False)}})
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        else:
            texts = []
            for p in parts:
                if p.function_response:
                    call_id = pending_ids.pop(0) if pending_ids else f"call_{next(counter)}"
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": json.dumps(p.function_response.response, ensure_ascii=False)})
                elif p.text:
                    texts.append(p.text)
            if texts:
                messages.append({"role": "user", "content": "\n".join(texts)})
    return messages


def to_chat_tools(tools: list[types.FunctionDeclaration]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description or "",
        "parameters": t.parameters_json_schema or {"type": "object", "properties": {}}}} for t in tools]


class OpenAICompatLLM:
    """Chat-completions API with tool calling (Groq, NVIDIA build, ...)."""

    def __init__(self, api_key: str, model: str, base_url: str, name: str = "openai-compatible",
                 transport: httpx.AsyncBaseTransport | None = None, retries: int = 1):
        if not api_key:
            raise LLMError(f"{name}: API key is not set")
        self.model, self.name, self.retries = model, name, retries
        self.last_model: str | None = None
        self.rate_limit_waits = 0
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport,
                                       headers={"Authorization": f"Bearer {api_key}"},
                                       timeout=httpx.Timeout(30.0, connect=5.0))

    async def step(self, system, contents, tools):
        body = {"model": self.model, "messages": to_chat_messages(system, contents),
                "tools": to_chat_tools(tools), "tool_choice": "required", "temperature": 0.2}
        last = ""
        for attempt in range(self.retries + 1):
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                last = f"network: {exc!r}"
                continue
            if resp.status_code == 429:
                raise LLMError(f"{self.name}: rate limited ({resp.text[:160]})", rate_limited=True)
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                await asyncio.sleep(1.0)
                continue
            if resp.status_code >= 400:
                raise LLMError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:200]}")
            message = resp.json()["choices"][0]["message"]
            parts = []
            for call in message.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                parts.append(types.Part.from_function_call(name=call["function"]["name"], args=args))
            if not parts and (message.get("content") or "").strip():
                parts.append(types.Part.from_text(text=message["content"].strip()))
            if parts:
                self.last_model = f"{self.name}:{self.model}"
                return types.Content(role="model", parts=parts)
            last = "empty response"
        raise LLMError(f"{self.name} unavailable: {last}")


class FallbackLLM:
    """Try each backend in order, so a Gemini rate limit falls through to a free backup."""

    def __init__(self, *backends):
        self.backends = [b for b in backends if b is not None]
        self.last_model: str | None = None

    @property
    def rate_limit_waits(self) -> int:
        return sum(getattr(b, "rate_limit_waits", 0) for b in self.backends)

    async def step(self, system, contents, tools):
        errors, rate_limited = [], False
        for backend in self.backends:
            try:
                content = await backend.step(system, contents, tools)
            except LLMError as exc:
                errors.append(str(exc)[:200])
                rate_limited = rate_limited or exc.rate_limited
                continue
            self.last_model = getattr(backend, "last_model", None)
            return content
        raise LLMError(" | ".join(errors) or "no LLM configured", rate_limited=rate_limited)
