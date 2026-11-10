import asyncio
import json

import httpx
import pytest
from google.genai import types

from app.dialog import TOOLS, DialogEngine
from app.llm import FallbackLLM, LLMError, OpenAICompatLLM, to_chat_messages, to_chat_tools

from .conftest import MONDAY_8AM
from .test_dialog import FakeLLM, say


def run(coro):
    return asyncio.run(coro)


def history():
    return [
        types.Content(role="model", parts=[types.Part.from_text(text="Namaste")]),
        types.Content(role="user", parts=[types.Part.from_text(text="Chandrakar kal?")]),
        types.Content(role="model", parts=[types.Part.from_function_call(name="find_slots", args={"doctor": "Chandrakar"})]),
        types.Content(role="user", parts=[types.Part.from_function_response(name="find_slots", response={"slots": []})]),
    ]


def test_history_converts_to_chat_messages_with_matching_tool_ids():
    msgs = to_chat_messages("SYSTEM", history())
    assert [m["role"] for m in msgs] == ["system", "assistant", "user", "assistant", "tool"]
    call = msgs[3]["tool_calls"][0]
    assert call["function"]["name"] == "find_slots"
    assert json.loads(call["function"]["arguments"]) == {"doctor": "Chandrakar"}
    assert msgs[4]["tool_call_id"] == call["id"] and json.loads(msgs[4]["content"]) == {"slots": []}


def test_tools_convert_to_chat_schema():
    tools = to_chat_tools(TOOLS)
    names = {t["function"]["name"] for t in tools}
    assert {"find_slots", "say", "confirm_pending"} <= names
    assert all(t["function"]["parameters"]["type"] == "object" for t in tools)


def client(handler):
    return OpenAICompatLLM("k", "test-model", "https://llm.example/v1", name="groq",
                           transport=httpx.MockTransport(handler))


def test_tool_call_response_becomes_function_call_parts():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "say", "arguments": json.dumps({"text": "Hi", "language": "en-IN"})}}]}}]})

    content = run(client(handler).step("SYS", history(), TOOLS))
    fc = content.parts[0].function_call
    assert fc.name == "say" and dict(fc.args) == {"text": "Hi", "language": "en-IN"}
    assert seen["body"]["tool_choice"] == "required" and seen["auth"] == "Bearer k"


def test_rate_limit_is_flagged():
    with pytest.raises(LLMError) as e:
        run(client(lambda r: httpx.Response(429, text="slow down")).step("S", history(), TOOLS))
    assert e.value.rate_limited


def test_fallback_uses_backup_when_primary_rate_limited():
    class Limited:
        last_model = None
        rate_limit_waits = 1

        async def step(self, *a):
            raise LLMError("429", rate_limited=True)

    backup = FakeLLM([say("from backup")])
    backup.last_model = "groq:x"
    llm = FallbackLLM(Limited(), backup)
    content = run(llm.step("S", [], TOOLS))
    assert content.parts[0].function_call.args["text"] == "from backup" and llm.last_model == "groq:x"


def test_fallback_reports_rate_limit_when_all_fail():
    class Limited:
        async def step(self, *a):
            raise LLMError("429", rate_limited=True)

    with pytest.raises(LLMError) as e:
        run(FallbackLLM(Limited(), Limited()).step("S", [], TOOLS))
    assert e.value.rate_limited


def test_rate_limited_turn_asks_to_repeat_without_staff_ticket(conn):
    eng = DialogEngine(FakeLLM(LLMError("429", rate_limited=True)), now_fn=lambda: MONDAY_8AM)
    sess, _ = eng.start(conn, "PAT-1020")
    r = run(eng.turn(conn, sess.id, "Clinic kab khulta hai?"))
    assert r.source == "busy" and not r.escalated
    assert conn.execute("SELECT COUNT(*) FROM followups WHERE call_id = ?", (sess.call_id,)).fetchone()[0] == 0


def test_failure_after_confirmation_still_reports_the_booking(conn):
    tue = "2026-11-17"
    eng = DialogEngine(FakeLLM(
        [("propose_booking", {"doctor": "Chandrakar", "date": tue, "time": "10:30"})], [say("confirm?")],
        [("confirm_pending", {})], LLMError("429", rate_limited=True)), now_fn=lambda: MONDAY_8AM)
    sess, _ = eng.start(conn, "PAT-1020")
    run(eng.turn(conn, sess.id, "Chandrakar tomorrow 10:30"))
    r = run(eng.turn(conn, sess.id, "haan"))
    assert r.card["type"] == "confirmed" and r.card["ref"] in r.reply
