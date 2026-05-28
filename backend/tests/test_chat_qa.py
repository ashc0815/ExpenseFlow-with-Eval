"""Commit 3 — Stateless Q&A agent endpoint smoke test.

Verifies：
  1. POST /api/chat/message returns an SSE stream that runs the unified
     expense assistant with the stateless QA tool surface.
  2. "这个月花了多少" triggers a get_spend_summary tool_call.
  3. Agent emits a final assistant_text summarizing the spend and ends.
  4. The whitelist is enforced at dispatch time: attempting to dispatch
     a blocked tool (via a stubbed MockLLM) returns an error in the
     tool_result payload instead of executing the handler.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", "/tmp/concurshield_qa_test")

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi.testclient import TestClient

from backend.db.store import Base, create_submission, get_db
from backend.main import app

_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)

async def _override_get_db():
    async with _Session() as session:
        yield session


def setup_module(_):
    import backend.config as _cfg
    _cfg.DATABASE_URL = _DB_URL

    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Seed 2 submissions for this month so summary is non-trivial
        today_iso = date.today().isoformat()
        async with _Session() as s:
            await create_submission(s, {
                "employee_id": "emp-qa",
                "status": "manager_approved",
                "amount": 150.00, "currency": "CNY",
                "category": "meal", "date": today_iso,
                "merchant": "海底捞 (测试)",
                "description": "团队午餐测试",
                "receipt_url": "/tmp/x.jpg",
                "invoice_number": "QA00000001",
            })
            await create_submission(s, {
                "employee_id": "emp-qa",
                "status": "exported",
                "amount": 88.00, "currency": "CNY",
                "category": "transport", "date": today_iso,
                "merchant": "滴滴 (测试)",
                "description": "打车去客户",
                "receipt_url": "/tmp/y.jpg",
                "invoice_number": "QA00000002",
            })

    asyncio.new_event_loop().run_until_complete(_init())
    app.dependency_overrides[get_db] = _override_get_db


def teardown_module(_):
    app.dependency_overrides.pop(get_db, None)
    try:
        asyncio.new_event_loop().run_until_complete(_engine.dispose())
    except Exception:
        pass
    try:
        os.unlink(_TMP_DB.name)
    except PermissionError:
        pass  # Windows: temp file still locked, OS Temp cleanup handles it


client = TestClient(app)
HEADERS = {"X-User-Id": "emp-qa", "X-User-Role": "employee"}


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def test_qa_spend_summary_flow():
    resp = client.post(
        "/api/chat/message",
        headers=HEADERS,
        json={"messages": [{"role": "user", "content": "我这个月花了多少钱"}]},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    kinds = [e["type"] for e in events]

    # Expected shape: message_start → assistant_text → tool_call → tool_result
    # → assistant_text (summary) → message_end
    assert "message_start" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert kinds[-1] == "message_end"

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "get_spend_summary"
    assert tool_calls[0]["input"] == {"period": "month"}

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["result"]["total"] == 238.0
    assert tool_results[0]["result"]["count"] == 2
    cats = {b["category"] for b in tool_results[0]["result"]["by_category"]}
    assert cats == {"meal", "transport"}

    final_texts = [e["text"] for e in events if e["type"] == "assistant_text"]
    # Final summary text mentions the total
    assert any("238" in t for t in final_texts)


def test_qa_default_welcome_for_unrelated_question():
    resp = client.post(
        "/api/chat/message",
        headers=HEADERS,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    kinds = [e["type"] for e in events]
    # No tool calls on unrecognized intent
    assert "tool_call" not in kinds
    assert kinds[-1] == "message_end"
    texts = [e["text"] for e in events if e["type"] == "assistant_text"]
    assert len(texts) > 0, "should have at least one assistant text response"


def test_identity_question_is_answered_by_backend_not_llm():
    from backend.api.routes import chat as chat_mod

    orig_get_llm = chat_mod.get_llm
    chat_mod.get_llm = lambda: (_ for _ in ()).throw(AssertionError("identity should not call LLM"))
    try:
        resp = client.post(
            "/api/chat/message",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "你是什么模型？"}]},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        kinds = [e["type"] for e in events]
        assert kinds == ["message_start", "assistant_text", "message_end"]
        text = " ".join(e.get("text", "") for e in events if e["type"] == "assistant_text")
        assert "ExpenseFlow" in text
        assert "不是 Claude" in text
        assert "Anthropic" not in text
    finally:
        chat_mod.get_llm = orig_get_llm


def test_qa_tool_whitelist_blocks_forbidden_dispatch():
    """Prompt-injection defense: even if the LLM hallucinates an update_draft_field
    tool_call, the stateless drawer rejects draft writes without a draft_id.
    """
    from backend.api.routes import chat as chat_mod

    class InjectedLLM(chat_mod.BaseLLM):
        """Pretends to be an LLM that's been prompt-injected into calling
        a forbidden write tool. Should be blocked at dispatch."""
        _called = False
        async def next_turn(self, messages, tools, agent_role="expense_assistant"):
            if not InjectedLLM._called:
                InjectedLLM._called = True
                return chat_mod.LLMResponse(
                    text="",
                    tool_calls=[{
                        "id": "tool_injected_01",
                        "name": "update_draft_field",
                        "input": {"field": "amount", "value": "999999", "source": "ocr"},
                    }],
                    stop_reason="tool_use",
                )
            return chat_mod.LLMResponse(text="done", stop_reason="end_turn")

    orig_get_llm = chat_mod.get_llm
    chat_mod.get_llm = lambda: InjectedLLM()
    try:
        resp = client.post(
            "/api/chat/message",
            headers=HEADERS,
            json={"messages": [{"role": "user", "content": "试一下"}]},
        )
        assert resp.status_code == 200
        events = _parse_sse(resp.text)

        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        result = tool_results[0]["result"]
        assert "error" in result
        assert "not allowed" in result["error"]
        assert "role" in result["error"]
        # And the tool name that got blocked is update_draft_field
        assert tool_results[0]["name"] == "update_draft_field"
    finally:
        chat_mod.get_llm = orig_get_llm
