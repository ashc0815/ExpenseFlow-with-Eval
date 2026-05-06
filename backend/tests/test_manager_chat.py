"""Manager copilot chat — drawer flow for manager / finance_admin roles.

Verifies the gap fix surfaced in the screenshot ("AI 报销助手 says it can't
find the report"):

  1. POST /api/chat/message with X-User-Role=manager routes through agent_role
     "manager" and gets the manager tool whitelist.
  2. Page-context injection works for managers viewing reports they don't own
     (the prior owner-check bug dropped context entirely).
  3. "为什么风险这么高" with a high-risk line in context triggers
     get_submission_for_review and surfaces fraud_signals + investigation.
  4. "待审队列" triggers get_pending_approval_queue scoped to the role's
     status (manager → pending; finance_admin → manager_approved).
  5. "团队本月花了多少" triggers get_team_spend_summary scoped to the
     manager's own department.
  6. Whitelist enforcement: a hallucinated update_draft_field call is
     rejected at dispatch even when caller is a manager.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", "/tmp/concurshield_mgr_test")

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi.testclient import TestClient

from backend.db.store import (
    Base, get_db, create_submission, upsert_employee, Report,
    list_report_submissions,
)
from backend.main import app

_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _Session() as session:
        yield session


_REPORT_ID = "rep-mgr-001"
_LOW_RISK_LINE_ID = "sub-mgr-low"
_HIGH_RISK_LINE_ID = "sub-mgr-high"
_REPORTER_ID = "emp-reporter"
_MANAGER_ID = "mgr-alice"


def setup_module(_):
    import backend.config as _cfg
    _cfg.DATABASE_URL = _DB_URL

    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        today_iso = date.today().isoformat()
        async with _Session() as s:
            await upsert_employee(s, {
                "id": _REPORTER_ID, "name": "王小报", "department": "产品部",
                "cost_center": "PRD-01",
            })
            await upsert_employee(s, {
                "id": _MANAGER_ID, "name": "Alice 经理", "department": "产品部",
                "cost_center": "PRD-01",
            })

            # Create a report with 2 submissions (one low-risk, one high-risk).
            r = Report(
                id=_REPORT_ID, employee_id=_REPORTER_ID,
                title="5月报销单", status="pending",
                submitted_at=datetime.now(timezone.utc),
            )
            s.add(r)
            await s.flush()

            await create_submission(s, {
                "id": _LOW_RISK_LINE_ID, "report_id": _REPORT_ID,
                "employee_id": _REPORTER_ID, "department": "产品部",
                "status": "reviewed", "tier": "T1", "risk_score": 25,
                "amount": 27.27, "currency": "USD",
                "category": "other", "date": "2020-10-17",
                "merchant": "Walmart", "description": "test",
                "receipt_url": "/tmp/x.jpg", "invoice_number": "MGR0001",
                "audit_report": {"fraud_signals": []},
            })
            await create_submission(s, {
                "id": _HIGH_RISK_LINE_ID, "report_id": _REPORT_ID,
                "employee_id": _REPORTER_ID, "department": "产品部",
                "status": "reviewed", "tier": "T4", "risk_score": 90,
                "amount": 15.80, "currency": "USD",
                "category": "meal", "date": "2024-11-21",
                "merchant": "Wendy's", "description": "周末加班餐",
                "receipt_url": "/tmp/y.jpg", "invoice_number": "MGR0002",
                "audit_report": {
                    "fraud_signals": [
                        {"rule_id": "weekend_frequency", "score": 70,
                         "evidence": "连续 4 个周末都在 Wendy's 报餐"},
                        {"rule_id": "merchant_repeat", "score": 60,
                         "evidence": "Wendy's 商家本月已出现 5 次"},
                    ],
                    "investigation": {
                        "verdict": "suspicious",
                        "confidence": 0.72,
                        "summary": "周末高频小额餐饮，疑似分单避限",
                    },
                },
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
        pass


client = TestClient(app)
MANAGER_HEADERS = {"X-User-Id": _MANAGER_ID, "X-User-Role": "manager"}
FINANCE_HEADERS = {"X-User-Id": "fin-bob",   "X-User-Role": "finance_admin"}


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


# ════════════════════════════════════════════════════════════════════
# 1. Role routing — manager ctx.role gets manager agent_role
# ════════════════════════════════════════════════════════════════════
def test_manager_role_routes_to_manager_agent():
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    texts = [e["text"] for e in events if e["type"] == "assistant_text"]
    # Manager welcome mentions audit / queue / team — distinct from employee welcome
    assert any("待审" in t or "审批" in t for t in texts), \
        f"Expected manager welcome text, got: {texts}"


# ════════════════════════════════════════════════════════════════════
# 2. Owner-check bypass — manager sees context for someone else's report
# ════════════════════════════════════════════════════════════════════
def test_manager_sees_context_for_other_employees_report():
    """Pre-fix bug: line 1994 dropped context_text when manager viewed a
    report not owned by them. After fix, manager+finance bypass that check.
    """
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={
            "messages": [{"role": "user", "content": "为什么风险这么高？"}],
            "context": {"report_id": _REPORT_ID},
        },
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    # The context injection fires on the agent's first turn — we can detect
    # it indirectly via the tool call: MockLLM picks the high-risk line
    # because the context lists risk= for each line.
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "get_submission_for_review"
    assert tool_calls[0]["input"]["submission_id"] == _HIGH_RISK_LINE_ID, \
        "Manager should investigate the highest-risk line, not the low-risk one"


# ════════════════════════════════════════════════════════════════════
# 3. "Why high risk" surfaces fraud_signals + investigation
# ════════════════════════════════════════════════════════════════════
def test_why_high_risk_returns_signals_and_investigation():
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={
            "messages": [{"role": "user", "content": "为什么风险这么高？"}],
            "context": {"report_id": _REPORT_ID},
        },
    )
    events = _parse_sse(resp.text)

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    payload = tool_results[0]["result"]
    assert payload["risk_score"] == 90
    assert payload["tier"] == "T4"
    assert payload["audit_report"]["fraud_signals"][0]["rule_id"] == "weekend_frequency"
    assert payload["audit_report"]["investigation"]["verdict"] == "suspicious"

    final_texts = [e["text"] for e in events if e["type"] == "assistant_text"]
    final = "\n".join(final_texts)
    # Surface the rule names + investigation verdict in chat text
    assert "weekend_frequency" in final or "merchant_repeat" in final
    assert "suspicious" in final or "调查" in final


# ════════════════════════════════════════════════════════════════════
# 4. Pending approval queue — manager sees pending reports
# ════════════════════════════════════════════════════════════════════
def test_pending_queue_for_manager():
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={"messages": [{"role": "user", "content": "我有哪些待审报销？"}]},
    )
    events = _parse_sse(resp.text)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "get_pending_approval_queue"

    tool_results = [e for e in events if e["type"] == "tool_result"]
    payload = tool_results[0]["result"]
    assert payload["queue_status"] == "pending"
    # Our seeded report is in pending state, so it must surface
    assert payload["total_count"] >= 1
    assert any(it["report_id"] == _REPORT_ID for it in payload["items"])


def test_pending_queue_min_risk_filter():
    """Ramp-style 'show me high-risk only' question."""
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={"messages": [{"role": "user", "content": "只看高风险的待审"}]},
    )
    events = _parse_sse(resp.text)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["input"].get("min_risk_score") == 80.0


def test_pending_queue_employee_role_denied():
    """Employee role does NOT have the manager queue tool — even by direct
    role-routing the chat is `employee` and that tool isn't in the whitelist.
    """
    headers = {"X-User-Id": _REPORTER_ID, "X-User-Role": "employee"}
    resp = client.post(
        "/api/chat/message",
        headers=headers,
        json={"messages": [{"role": "user", "content": "我有哪些待审报销"}]},
    )
    events = _parse_sse(resp.text)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    # MockLLM's _qa_turn (employee) doesn't recognize "待审" as an intent
    # — at most it returns a welcome. Critically, no get_pending_approval_queue
    # ever fires for an employee.
    for tc in tool_calls:
        assert tc["name"] != "get_pending_approval_queue"


# ════════════════════════════════════════════════════════════════════
# 5. Team spend — manager scoped to their own department
# ════════════════════════════════════════════════════════════════════
def test_team_spend_scoped_to_manager_department():
    resp = client.post(
        "/api/chat/message",
        headers=MANAGER_HEADERS,
        json={"messages": [{"role": "user", "content": "我团队本月花了多少？"}]},
    )
    events = _parse_sse(resp.text)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "get_team_spend_summary"

    tool_results = [e for e in events if e["type"] == "tool_result"]
    payload = tool_results[0]["result"]
    assert payload["department"] == "产品部"
    assert payload["department_scope"] == "self"


# ════════════════════════════════════════════════════════════════════
# 6. Whitelist still blocks writes even for manager
# ════════════════════════════════════════════════════════════════════
def test_manager_whitelist_blocks_write_tools():
    from backend.api.routes import chat as chat_mod

    class InjectedLLM(chat_mod.BaseLLM):
        _called = False
        async def next_turn(self, messages, tools, agent_role="employee_submit"):
            if not InjectedLLM._called:
                InjectedLLM._called = True
                return chat_mod.LLMResponse(
                    text="",
                    tool_calls=[{
                        "id": "tool_inj_mgr_01",
                        "name": "update_report_line_field",
                        "input": {"line_id": _HIGH_RISK_LINE_ID, "field": "amount", "value": "0"},
                    }],
                    stop_reason="tool_use",
                )
            return chat_mod.LLMResponse(text="done", stop_reason="end_turn")

    orig_get_llm = chat_mod.get_llm
    chat_mod.get_llm = lambda: InjectedLLM()
    try:
        resp = client.post(
            "/api/chat/message",
            headers=MANAGER_HEADERS,
            json={"messages": [{"role": "user", "content": "把那笔金额改成 0"}]},
        )
        events = _parse_sse(resp.text)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert len(tool_results) == 1
        result = tool_results[0]["result"]
        assert "error" in result
        assert "not allowed" in result["error"]
        assert result["error"].endswith("'manager'")
    finally:
        chat_mod.get_llm = orig_get_llm
