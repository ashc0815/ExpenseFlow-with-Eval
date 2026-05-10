"""Day 4 — Agent Eval Harness.

Parameterized test cases from eval_cases.yaml, covering all 3 agent forms:

  employee          (4 cases)  intent routing + tool whitelist
  manager_explain   (5 cases)  tier→recommendation + role ACL
  whitelist_inject  (3 cases)  prompt-injection defense

Run:
  pytest backend/tests/test_agent_eval.py -v         # see per-case pass/fail
  pytest backend/tests/test_agent_eval.py -v -s      # also see the pass-rate table

Pass rate is always written to stderr at the end of the module so it appears
even without -s.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from fastapi.testclient import TestClient

# ── Temp DB (must be set before importing app) ────────────────────────────────
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", "/tmp/concurshield_eval_test")

from backend.db.store import Base, create_submission, get_db, update_submission_analysis
from backend.main import app

_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _Session() as session:
        yield session


# ── Seeded submission IDs (populated in setup_module) ─────────────────────────
_SUB_IDS: dict[str, str] = {}

# ── Per-case result tracking for pass-rate summary ───────────────────────────
_EVAL_RESULTS: dict[str, bool] = {}

# ── Load YAML cases ───────────────────────────────────────────────────────────
_CASES_PATH = Path(__file__).parent / "eval_cases.yaml"
with open(_CASES_PATH, encoding="utf-8") as _f:
    _CASES: list[dict] = yaml.safe_load(_f)


# ── Module setup / teardown ───────────────────────────────────────────────────

def setup_module(_):
    import backend.config as _cfg
    _cfg.DATABASE_URL = _DB_URL

    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with _Session() as s:
            today = date.today()

            # QA history: 2 submissions for emp-eval-qa (150 + 88 = 238 CNY this month)
            for inv, amt, cat, merchant in [
                ("EVAL00000001", 150.0, "meal",      "海底捞 (eval)"),
                ("EVAL00000002",  88.0, "transport", "滴滴 (eval)"),
            ]:
                await create_submission(s, {
                    "employee_id": "emp-eval-qa",
                    "status": "manager_approved",
                    "amount": amt, "currency": "CNY",
                    "category": cat, "date": today.isoformat(),
                    "merchant": merchant,
                    "description": f"eval 测试报销 {inv}",
                    "receipt_url": "/tmp/eval.jpg",
                    "invoice_number": inv,
                })

            # T1 — low risk, all timeline steps passed
            sub1 = await create_submission(s, {
                "employee_id": "emp-eval-t1",
                "status": "reviewed",
                "amount": 120.0, "currency": "CNY",
                "category": "meal", "date": today.isoformat(),
                "merchant": "肯德基 (eval)",
                "description": "团队午餐讨论 AI 评审机制",
                "receipt_url": "/tmp/eval.jpg",
                "invoice_number": "EVAL00000010",
            })
            await update_submission_analysis(s, sub1.id,
                tier="T1", risk_score=18.0,
                audit_report={
                    "final_status": "completed",
                    "timeline": [
                        {"message": "发票字段验证通过", "passed": True, "skipped": False},
                        {"message": "金额未超类别限额",  "passed": True, "skipped": False},
                        {"message": "合规检查通过",      "passed": True, "skipped": False},
                    ],
                    "shield_report": {"total_score": 5, "triggered": []},
                },
                status="reviewed",
            )
            _SUB_IDS["T1"] = sub1.id

            # T3 — medium risk: one failed step + shield score ≥ 30
            sub3 = await create_submission(s, {
                "employee_id": "emp-eval-t3",
                "status": "reviewed",
                "amount": 450.0, "currency": "CNY",
                "category": "entertainment", "date": today.isoformat(),
                "merchant": "大众娱乐会所 (eval)",
                "description": "客户招待餐饮及娱乐",
                "receipt_url": "/tmp/eval.jpg",
                "invoice_number": "EVAL00000030",
            })
            await update_submission_analysis(s, sub3.id,
                tier="T3", risk_score=68.0,
                audit_report={
                    "final_status": "pending_review",
                    "timeline": [
                        {"message": "发票字段验证通过",         "passed": True,  "skipped": False},
                        {"message": "金额接近娱乐类别限额上限", "passed": False, "skipped": False},
                    ],
                    "shield_report": {"total_score": 42, "triggered": [
                        {"message": "金额位于类别上限 90-110% 区间"},
                    ]},
                },
                status="reviewed",
            )
            _SUB_IDS["T3"] = sub3.id

            # T4 — high risk: failed step + high shield score
            sub4 = await create_submission(s, {
                "employee_id": "emp-eval-t4",
                "status": "reviewed",
                "amount": 5500.0, "currency": "CNY",
                "category": "entertainment", "date": today.isoformat(),
                "merchant": "未知娱乐场所 (eval)",
                "description": "招待",
                "receipt_url": "/tmp/eval.jpg",
                "invoice_number": "EVAL00000040",
            })
            await update_submission_analysis(s, sub4.id,
                tier="T4", risk_score=91.0,
                audit_report={
                    "final_status": "rejected",
                    "timeline": [
                        {"message": "发票字段验证通过",        "passed": True,  "skipped": False},
                        {"message": "金额超娱乐类别限额 200%", "passed": False, "skipped": False},
                    ],
                    "shield_report": {"total_score": 80, "triggered": [
                        {"message": "金额位于类别上限附近"},
                        {"message": "费用描述过于简短"},
                    ]},
                },
                status="reviewed",
            )
            _SUB_IDS["T4"] = sub4.id

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

    # Always write the pass-rate summary to stderr (visible even without -s)
    total = len(_EVAL_RESULTS)
    passed_n = sum(1 for v in _EVAL_RESULTS.values() if v)
    rate = (passed_n / total * 100) if total else 0.0

    lines = [
        "",
        "═" * 56,
        "  AGENT EVAL HARNESS — RESULTS",
        "═" * 56,
    ]
    for cid, ok in _EVAL_RESULTS.items():
        lines.append(f"  {'✓' if ok else '✗'}  {cid}")
    bar = "█" * passed_n + "░" * (total - passed_n)
    lines += [
        "─" * 56,
        f"  [{bar}]  {passed_n}/{total} passed ({rate:.0f}%)",
        "═" * 56,
        "",
    ]
    sys.stderr.write("\n".join(lines) + "\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

_ROLE_HEADERS: dict[str, dict] = {
    "employee":      {"X-User-Id": "emp-eval-qa", "X-User-Role": "employee"},
    "manager":       {"X-User-Id": "mgr-eval",    "X-User-Role": "manager"},
    "finance_admin": {"X-User-Id": "fin-eval",    "X-User-Role": "finance_admin"},
}


def _parse_sse(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[len("data: "):]))
            except json.JSONDecodeError:
                pass
    return events


# ── Main parametrized test ────────────────────────────────────────────────────

client = TestClient(app)


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_eval_case(case: dict[str, Any]) -> None:  # noqa: C901
    case_id: str = case["id"]
    expect: dict = case.get("expect", {})
    agent: str = case["agent"]
    role: str = case.get("role", "employee")
    headers = _ROLE_HEADERS[role]

    try:
        if agent == "employee":
            _run_qa_case(case, headers, expect)

        elif agent == "manager_explain":
            _run_explain_case(case, headers, expect)

        elif agent == "whitelist_inject":
            _run_whitelist_case(case, headers, expect)

        else:
            pytest.skip(f"Unknown agent type: '{agent}'")

    except AssertionError:
        _EVAL_RESULTS[case_id] = False
        raise
    else:
        _EVAL_RESULTS[case_id] = True


# ── Case runners ──────────────────────────────────────────────────────────────

def _run_qa_case(case: dict, headers: dict, expect: dict) -> None:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": case["message"]}]}
    if case.get("context"):
        body["context"] = case["context"]
    resp = client.post(
        "/api/chat/message",
        headers=headers,
        json=body,
    )
    expected_status = expect.get("http_status", 200)
    assert resp.status_code == expected_status, \
        f"HTTP {resp.status_code} ≠ {expected_status}: {resp.text[:300]}"

    events = _parse_sse(resp.text)
    tool_names = [e["name"] for e in events if e["type"] == "tool_call"]

    for required in (expect.get("tool_calls_include") or []):
        assert required in tool_names, \
            f"Expected tool '{required}' in calls {tool_names}"

    for forbidden in (expect.get("tool_calls_exclude") or []):
        assert forbidden not in tool_names, \
            f"Forbidden tool '{forbidden}' appeared in calls {tool_names}"

    all_text = " ".join(
        e.get("text", "") for e in events if e["type"] == "assistant_text"
    )
    for phrase in (expect.get("response_contains") or []):
        assert phrase in all_text, \
            f"Expected '{phrase}' in response: {all_text[:400]}"


def _run_explain_case(case: dict, headers: dict, expect: dict) -> None:
    tier_key = case.get("tier_key", "T1")
    sub_id = _SUB_IDS.get(tier_key, "nonexistent-id-000")

    resp = client.post(f"/api/chat/explain/{sub_id}", headers=headers)
    expected_status = expect.get("http_status", 200)
    assert resp.status_code == expected_status, \
        f"HTTP {resp.status_code} ≠ {expected_status}: {resp.text[:300]}"

    if expected_status != 200:
        return  # ACL / error cases: HTTP status check is sufficient

    body = resp.json()

    if "recommendation" in expect:
        assert body["recommendation"] == expect["recommendation"], \
            f"recommendation '{body['recommendation']}' ≠ '{expect['recommendation']}'"

    if "green_flags_min" in expect:
        assert len(body.get("green_flags", [])) >= expect["green_flags_min"], \
            f"green_flags {body.get('green_flags')} < min {expect['green_flags_min']}"

    if "red_flags_min" in expect:
        assert len(body.get("red_flags", [])) >= expect["red_flags_min"], \
            f"red_flags {body.get('red_flags')} < min {expect['red_flags_min']}"

    if "advisory_contains" in expect:
        advisory = body.get("advisory") or ""
        assert expect["advisory_contains"] in advisory, \
            f"advisory '{advisory}' doesn't contain '{expect['advisory_contains']}'"


def _run_whitelist_case(case: dict, headers: dict, expect: dict) -> None:
    from backend.api.routes import chat as chat_mod

    forbidden_tool: str = case["inject_forbidden_tool"]
    tool_input: dict = case.get("inject_tool_input") or {}
    expected_blocked: str = expect.get("blocked_tool", forbidden_tool)

    class _InjectedLLM(chat_mod.BaseLLM):
        def __init__(self) -> None:
            self._fired = False

        async def next_turn(
            self, messages: list, tools: list, agent_role: str = "employee_submit"
        ) -> chat_mod.LLMResponse:
            if not self._fired:
                self._fired = True
                return chat_mod.LLMResponse(
                    text="",
                    tool_calls=[{
                        "id": "tool_injected_eval_01",
                        "name": forbidden_tool,
                        "input": tool_input,
                    }],
                    stop_reason="tool_use",
                )
            return chat_mod.LLMResponse(text="done", stop_reason="end_turn")

    orig_get_llm = chat_mod.get_llm
    chat_mod.get_llm = lambda: _InjectedLLM()
    try:
        resp = client.post(
            "/api/chat/message",
            headers=headers,
            json={"messages": [{"role": "user", "content": "test injection"}]},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        events = _parse_sse(resp.text)
        tool_results = [e for e in events if e["type"] == "tool_result"]
        assert tool_results, "No tool_result events found in SSE stream"

        # Find the tool_result for the blocked tool
        blocked_tr = next(
            (tr for tr in tool_results if tr.get("name") == expected_blocked),
            None,
        )
        assert blocked_tr is not None, \
            f"No tool_result for '{expected_blocked}' found; got {[tr.get('name') for tr in tool_results]}"

        error_text: str = blocked_tr["result"].get("error", "")
        expected_phrase: str = expect.get("whitelist_error_contains", "not allowed")
        assert expected_phrase in error_text, \
            f"Expected '{expected_phrase}' in error: '{error_text}'"
    finally:
        chat_mod.get_llm = orig_get_llm
