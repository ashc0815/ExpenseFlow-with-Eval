"""Unified Expense Assistant eval harness.

This suite evaluates the chat/tool layer for:
  - policy QA
  - missing / blurry receipt completion through external evidence tools
  - long-tail bad cases and safety boundaries
  - model-matrix style aggregation: quality, latency proxy, tool count, cost proxy

Run:
  pytest backend/tests/test_chatbot_eval.py -q
  CHATBOT_EVAL_MODELS=mock-scripted-baseline,mock-scripted-cheap pytest backend/tests/test_chatbot_eval.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


# ── Temp DB (must be set before importing app/db modules) ────────────────────
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", "/tmp/expenseflow_unified_expense_assistant_eval")

from backend.api.middleware.auth import UserContext
from backend.api.routes import chat as chat_mod
from backend.db.store import Base, create_draft, get_draft
from backend.tests.graders.code_graders import (
    assistant_text,
    event_subagents,
    event_tool_names,
    grade_field_sources_include,
    grade_fields_absent,
    grade_final_fields,
    grade_forbidden_tools_absent,
    grade_agent_trace_present,
    grade_must_call_tools,
    grade_response_contains,
    grade_response_excludes,
    grade_required_subagents,
    grade_tool_args,
    grade_trace_shape,
)


_TEST_DIR = Path(__file__).resolve().parent
_DATASET_PATH = _TEST_DIR / "eval_datasets" / "chatbot_expense_assistant.yaml"
_DATASET_SETS_PATH = _TEST_DIR / "eval_datasets" / "chatbot_dataset_sets.yaml"
_OUT_PATH = _TEST_DIR / "eval_chatbot_model_matrix_latest.json"

_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _load_cases() -> list[dict]:
    cases = yaml.safe_load(_DATASET_PATH.read_text(encoding="utf-8")) or []
    dataset_sets = _load_dataset_sets()
    requested = [
        name.strip()
        for name in os.getenv("CHATBOT_EVAL_DATASETS", "").split(",")
        if name.strip()
    ]
    if requested:
        allowed = set(requested)
        cases = [c for c in cases if _case_matches_dataset_filter(c, allowed, dataset_sets)]
    return cases


def _load_dataset_sets() -> dict[str, dict]:
    if not _DATASET_SETS_PATH.exists():
        return {}
    rows = yaml.safe_load(_DATASET_SETS_PATH.read_text(encoding="utf-8")) or []
    return {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and row.get("id")
    }


def _case_matches_dataset_filter(case: dict, allowed: set[str], dataset_sets: dict[str, dict]) -> bool:
    direct_values = {
        str(case.get("id") or ""),
        str(case.get("suite") or ""),
        str(case.get("scenario") or ""),
        str(case.get("dataset") or ""),
        str(case.get("case_set") or ""),
    }
    if direct_values & allowed:
        return True

    case_tags = set(str(tag) for tag in (case.get("tags") or []))
    if case_tags & allowed:
        return True

    for name in allowed:
        spec = dataset_sets.get(name)
        if not spec:
            continue
        if case.get("suite") in set(spec.get("suites") or []):
            return True
        if case.get("scenario") in set(spec.get("scenarios") or []):
            return True
        if case.get("id") in set(spec.get("case_ids") or []):
            return True
        if case_tags & set(str(tag) for tag in (spec.get("tags") or [])):
            return True
    return False


def _requested_dataset_names() -> list[str]:
    return [
        name.strip()
        for name in os.getenv("EVAL_TRIGGER_DATASETS", os.getenv("CHATBOT_EVAL_DATASETS", "")).split(",")
        if name.strip()
    ]


def test_all_scenarios1_registry_is_frozen_baseline() -> None:
    dataset_sets = _load_dataset_sets()
    spec = dataset_sets.get("all_scenarios1")
    assert spec is not None
    assert spec.get("baseline") is True
    assert spec.get("baseline_version") == "all_scenarios1-v1"

    all_cases = yaml.safe_load(_DATASET_PATH.read_text(encoding="utf-8")) or []
    matching_cases = [
        case
        for case in all_cases
        if _case_matches_dataset_filter(case, {"all_scenarios1"}, dataset_sets)
    ]
    assert len(matching_cases) == spec.get("expected_case_count") == 30


_CASES = _load_cases()
_MODEL_NAMES = [
    name.strip()
    for name in os.getenv("CHATBOT_EVAL_MODELS", "mock-scripted-baseline").split(",")
    if name.strip()
]

_MODEL_PROFILES = {
    # Cost numbers are proxy USD / 1M tokens so the aggregator can compare ROI
    # shape without depending on live vendor pricing during offline tests.
    "mock-scripted-baseline": {"input_cost_per_m": 0.15, "output_cost_per_m": 0.60, "latency_multiplier": 1.0},
    "mock-scripted-cheap": {"input_cost_per_m": 0.05, "output_cost_per_m": 0.20, "latency_multiplier": 0.7},
    "mock-scripted-premium": {"input_cost_per_m": 3.00, "output_cost_per_m": 15.00, "latency_multiplier": 1.4},
    # Real model profiles — latency_multiplier=1.0 because real API latency is measured directly
    "gpt-4o-mini": {"input_cost_per_m": 0.15, "output_cost_per_m": 0.60, "latency_multiplier": 1.0},
    "gpt-4o": {"input_cost_per_m": 2.50, "output_cost_per_m": 10.00, "latency_multiplier": 1.0},
    "deepseek-v4-pro": {"input_cost_per_m": 0.44, "output_cost_per_m": 0.87, "latency_multiplier": 1.0},
    "deepseek-v4-flash": {"input_cost_per_m": 0.14, "output_cost_per_m": 0.28, "latency_multiplier": 1.0},
}

# Frontend display name → API model name + provider config.
# The eval dashboard sends display names (e.g. "Deepseek V4"); this map
# resolves them to actual API model IDs and connection params.
_MODEL_NAME_MAP: dict[str, dict] = {
    # DeepSeek V4
    "DeepSeek V4": {"api_model": "deepseek-v4-pro", "provider": "deepseek"},
    "DeepSeek V4 Pro": {"api_model": "deepseek-v4-pro", "provider": "deepseek"},
    "DeepSeek V4 Flash": {"api_model": "deepseek-v4-flash", "provider": "deepseek"},
    "Deepseek V4": {"api_model": "deepseek-v4-pro", "provider": "deepseek"},
    "Deepseek V4 Pro": {"api_model": "deepseek-v4-pro", "provider": "deepseek"},
    "Deepseek V4 Flash": {"api_model": "deepseek-v4-flash", "provider": "deepseek"},
    # Direct API model names also work
    "deepseek-v4-pro": {"api_model": "deepseek-v4-pro", "provider": "deepseek"},
    "deepseek-v4-flash": {"api_model": "deepseek-v4-flash", "provider": "deepseek"},
    # OpenAI
    "GPT-4o-mini": {"api_model": "gpt-4o-mini", "provider": "openai"},
    "GPT-4o": {"api_model": "gpt-4o", "provider": "openai"},
    "OpenAI-4o-mini": {"api_model": "gpt-4o-mini", "provider": "openai"},
    "OpenAI-4o": {"api_model": "gpt-4o", "provider": "openai"},
    "gpt-4o-mini": {"api_model": "gpt-4o-mini", "provider": "openai"},
    "gpt-4o": {"api_model": "gpt-4o", "provider": "openai"},
}


def _resolve_model(display_name: str) -> tuple[str, str]:
    """Resolve display name → (api_model, provider)."""
    entry = _MODEL_NAME_MAP.get(display_name)
    if entry:
        return entry["api_model"], entry["provider"]
    if "deepseek" in display_name.lower():
        return display_name, "deepseek"
    return display_name, "openai"


def _is_real_model(model_name: str) -> bool:
    return not model_name.startswith("mock-scripted")


def _make_eval_llm(model_name: str, case: dict) -> chat_mod.BaseLLM:
    """Route model name to the appropriate LLM backend."""
    if not _is_real_model(model_name):
        return ScriptedEvalLLM(case, model_name)
    api_model, provider = _resolve_model(model_name)
    if provider == "deepseek":
        return chat_mod.RealLLM(
            model=api_model,
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
    return chat_mod.RealLLM(model=api_model)

_RESULTS: list[dict] = []
_RUN_START = datetime.now(timezone.utc)
_ALLOW_FAILURES = os.getenv("CHATBOT_EVAL_ALLOW_FAILURES", "").lower() in {"1", "true", "yes"}


def setup_module(_: Any) -> None:
    async def _init() -> None:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.new_event_loop().run_until_complete(_init())


def teardown_module(_: Any) -> None:
    try:
        _write_matrix_snapshot()
    finally:
        try:
            asyncio.new_event_loop().run_until_complete(_engine.dispose())
        except Exception:
            pass
        try:
            os.unlink(_TMP_DB.name)
        except PermissionError:
            pass


class ScriptedEvalLLM(chat_mod.BaseLLM):
    """Deterministic LLM driver for eval cases.

    It plays the case's scripted turns through the real run_agent loop, so tool
    whitelist enforcement, dispatch, draft writes, SSE events, and graders are
    all exercised exactly like production.
    """

    def __init__(self, case: dict, model_name: str) -> None:
        self.case = case
        self.model_name = model_name
        self.turn_index = 0

    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "expense_assistant",
    ) -> chat_mod.LLMResponse:
        turns = self.case.get("scripted_turns") or []
        if self.turn_index < len(turns):
            turn = turns[self.turn_index]
            self.turn_index += 1
            tool_calls = []
            for raw in turn.get("tool_calls") or []:
                tool_calls.append({
                    "id": f"eval_{uuid.uuid4().hex[:12]}",
                    "name": raw["name"],
                    "input": raw.get("input") or {},
                })
            text = turn.get("text", "")
            if not tool_calls:
                final = self.case.get("final_text", "")
                if final and final not in text:
                    text = f"{text}\n{final}" if text else final
                self.turn_index = len(turns) + 1
            return chat_mod.LLMResponse(
                text=text,
                tool_calls=tool_calls,
                stop_reason="tool_use" if tool_calls else "end_turn",
            )
        if self.turn_index == len(turns):
            self.turn_index += 1
            return chat_mod.LLMResponse(
                text=self.case.get("final_text", ""),
                stop_reason="end_turn",
            )
        return chat_mod.LLMResponse(text="", stop_reason="end_turn")


@pytest.mark.parametrize("model_name", _MODEL_NAMES)
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_chatbot_eval_case(case: dict, model_name: str) -> None:
    if _is_real_model(model_name):
        _, provider = _resolve_model(model_name)
        if provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            pytest.skip("DEEPSEEK_API_KEY not set")
        elif provider == "openai" and not os.getenv("OPENAI_API_KEY"):
            pytest.skip("OPENAI_API_KEY not set")
    result = asyncio.new_event_loop().run_until_complete(_run_case(case, model_name))
    _RESULTS.append(result)
    if not result["passed"] and not _ALLOW_FAILURES:
        detail = "\n".join(
            f"- {g['name']}: {g['message']}"
            for g in result["graders"]
            if not g["passed"]
        )
        pytest.fail(f"{case['id']} failed for {model_name}:\n{detail}")


async def _run_case(case: dict, model_name: str) -> dict:
    agent_role = "expense_assistant"
    ctx = UserContext(user_id="emp-chatbot-eval", roles=["employee"])
    start = time.perf_counter()
    events: list[dict] = []
    draft_fields: dict = {}
    field_sources: dict = {}

    orig_get_llm = chat_mod.get_llm
    orig_case_id = os.environ.get("EXPENSE_EVAL_CASE_ID")
    chat_mod.get_llm = lambda: _make_eval_llm(model_name, case)
    os.environ["EXPENSE_EVAL_CASE_ID"] = str(case.get("id") or "")
    try:
        async with _Session() as db:
            if _case_needs_draft(case):
                draft = await create_draft(db, ctx.user_id)
                draft_id = draft.id
                user_message = _latest_user_message(case)
                async for event in chat_mod.run_agent(
                    user_message=user_message,
                    draft_id=draft_id,
                    ctx=ctx,
                    db=db,
                    agent_role=agent_role,
                ):
                    events.append(event)
                fresh = await get_draft(db, draft_id)
                draft_fields = dict(fresh.fields or {}) if fresh else {}
                field_sources = dict(fresh.field_sources or {}) if fresh else {}
                if not draft_fields:
                    draft_fields, field_sources = _draft_fields_from_events(events)
            else:
                async for event in chat_mod.run_agent(
                    user_message="",
                    draft_id=None,
                    ctx=ctx,
                    db=db,
                    agent_role=agent_role,
                    messages_history=case.get("messages") or [],
                ):
                    events.append(event)
    finally:
        chat_mod.get_llm = orig_get_llm
        if orig_case_id is None:
            os.environ.pop("EXPENSE_EVAL_CASE_ID", None)
        else:
            os.environ["EXPENSE_EVAL_CASE_ID"] = orig_case_id

    latency_ms = int((time.perf_counter() - start) * 1000 * _latency_multiplier(model_name))
    graders = _grade_result(case, events, draft_fields, field_sources, is_real_model=_is_real_model(model_name))
    passed = all(g["passed"] for g in graders)
    input_tokens, output_tokens = _estimate_tokens(case, events)

    inferred_label = _infer_decision_label(events, draft_fields)

    return {
        "case_id": case["id"],
        "suite": case.get("suite"),
        "scenario": case.get("scenario"),
        "difficulty": case.get("difficulty"),
        "tags": case.get("tags", []),
        "model": model_name,
        "passed": passed,
        "graders": graders,
        "decision_label_expected": (case.get("expect") or {}).get("decision_label"),
        "decision_label_actual": inferred_label,
        "tool_calls": event_tool_names(events),
        "subagents": event_subagents(events),
        "agent_trace_steps": _agent_trace_steps(events),
        "tool_call_count": len(event_tool_names(events)),
        "assistant_text": assistant_text(events),
        "draft_fields": draft_fields,
        "field_sources": field_sources,
        "latency_ms": latency_ms,
        "token_usage_estimate": {"input": input_tokens, "output": output_tokens},
        "cost_estimate_usd": _estimate_cost(model_name, input_tokens, output_tokens),
    }


def _draft_fields_from_events(events: list[dict]) -> tuple[dict, dict]:
    for event in reversed(events):
        if event.get("type") == "draft_updated" and isinstance(event.get("fields"), dict):
            return dict(event.get("fields") or {}), dict(event.get("field_sources") or {})

    fields: dict = {}
    sources: dict = {}
    for event in events:
        if event.get("type") != "tool_result" or event.get("name") != "update_draft_field":
            continue
        result = event.get("result") or {}
        if not isinstance(result, dict) or result.get("deferred_to") or not result.get("ok"):
            continue
        field = result.get("field")
        if not field:
            continue
        fields[str(field)] = result.get("value")
        sources[str(field)] = result.get("source")
    return fields, sources


def _case_needs_draft(case: dict) -> bool:
    return case.get("scenario") == "receipt_completion"


def _latest_user_message(case: dict) -> str:
    for msg in reversed(case.get("messages") or []):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _infer_decision_label(events: list[dict], draft_fields: dict) -> str:
    """Infer the agent's business decision from its behavior.

    PASS:             agent called update_draft_field and wrote fields
    FLAG_FOR_HUMAN:   agent did not write, response asks user for info
    REJECT:           agent did not write, response explains a blocker
    """
    tool_names = event_tool_names(events)
    wrote = "update_draft_field" in tool_names and bool(draft_fields)
    if wrote:
        return "PASS"
    text = assistant_text(events).lower()
    reject_signals = ["已取消", "cancelled", "blocked", "不符", "不能报销", "无法报销", "拒绝报销", "净额为0", "net=0"]
    if any(s in text for s in reject_signals):
        return "REJECT"
    return "FLAG_FOR_HUMAN"


def _grade_decision_label(
    events: list[dict], draft_fields: dict, expected_label: str,
) -> tuple[bool, str]:
    actual = _infer_decision_label(events, draft_fields)
    passed = actual == expected_label
    return passed, f"actual={actual} {'==' if passed else '!='} expected={expected_label}"


def _grade_result(
    case: dict,
    events: list[dict],
    draft_fields: dict,
    field_sources: dict,
    *,
    is_real_model: bool = False,
) -> list[dict]:
    expect = case.get("expect") or {}
    checks: list[tuple[str, bool, str]] = []

    checks.append(("must_call_tools", *grade_must_call_tools(events, expect.get("must_call_tools") or [])))
    checks.append(("forbidden_tools_absent", *grade_forbidden_tools_absent(events, expect.get("forbidden_tools") or [])))

    if not is_real_model:
        checks.append(("response_contains", *grade_response_contains(events, expect.get("response_contains") or [])))
        checks.append(("response_excludes", *grade_response_excludes(events, expect.get("response_not_contains") or [])))

    if expect.get("decision_label"):
        checks.append(("decision_label", *_grade_decision_label(events, draft_fields, expect["decision_label"])))

    if expect.get("required_subagents"):
        checks.append(("required_subagents", *grade_required_subagents(events, expect["required_subagents"])))
    if expect.get("agent_trace_present") is not None:
        checks.append(("agent_trace_present", *grade_agent_trace_present(events, bool(expect["agent_trace_present"]))))
    if expect.get("final_fields"):
        checks.append(("final_fields", *grade_final_fields(draft_fields, expect["final_fields"])))
    if expect.get("final_fields_absent"):
        checks.append(("final_fields_absent", *grade_fields_absent(draft_fields, expect["final_fields_absent"])))
    if expect.get("field_sources_include"):
        checks.append(("field_sources_include", *grade_field_sources_include(field_sources, expect["field_sources_include"])))
    if expect.get("tool_args"):
        checks.append(("tool_args", *grade_tool_args(events, expect["tool_args"])))
    if expect.get("trace_shape_tools"):
        checks.append(("trace_shape", *grade_trace_shape(events, expect["trace_shape_tools"])))
    if expect.get("no_hallucinated_policy"):
        called_policy = "get_policy_rules" in event_tool_names(events)
        checks.append(("no_hallucinated_policy", called_policy, f"called_policy={called_policy}"))

    return [
        {"name": name, "passed": passed, "message": message}
        for name, passed, message in checks
    ]


def _agent_trace_steps(events: list[dict]) -> list[dict]:
    for event in reversed(events):
        if event.get("type") == "agent_trace":
            return event.get("steps") or []
    return []


def _estimate_tokens(case: dict, events: list[dict]) -> tuple[int, int]:
    input_chars = len(json.dumps(case.get("messages") or [], ensure_ascii=False))
    output_chars = len(assistant_text(events))
    # Offline approximation. The exact vendor token count is captured by
    # LLMTrace when real models are used; this keeps mock matrix comparable.
    return max(1, input_chars // 4), max(1, output_chars // 4)


def _model_profile(model_name: str) -> dict:
    """Resolve display name to cost/latency profile."""
    if model_name in _MODEL_PROFILES:
        return _MODEL_PROFILES[model_name]
    api_model, _ = _resolve_model(model_name)
    return _MODEL_PROFILES.get(api_model, _MODEL_PROFILES["mock-scripted-baseline"])


def _latency_multiplier(model_name: str) -> float:
    return float(_model_profile(model_name).get("latency_multiplier", 1.0))


def _estimate_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    profile = _model_profile(model_name)
    cost = (
        input_tokens / 1_000_000 * profile["input_cost_per_m"]
        + output_tokens / 1_000_000 * profile["output_cost_per_m"]
    )
    return round(cost, 8)


def _write_matrix_snapshot() -> None:
    finished = datetime.now(timezone.utc)
    by_model: dict[str, dict] = {}
    by_suite: dict[str, dict] = {}
    requested_datasets = _requested_dataset_names()
    dataset_name = ",".join(requested_datasets) if requested_datasets else "full_chatbot_expense_assistant"

    for model in sorted({r["model"] for r in _RESULTS}):
        rows = [r for r in _RESULTS if r["model"] == model]
        by_model[model] = _aggregate_rows(rows)

    for suite in sorted({r.get("suite") or "unknown" for r in _RESULTS}):
        rows = [r for r in _RESULTS if (r.get("suite") or "unknown") == suite]
        by_suite[suite] = _aggregate_rows(rows)

    payload = {
        "started_at": _RUN_START.isoformat(),
        "finished_at": finished.isoformat(),
        "dataset": str(_DATASET_PATH.name),
        "dataset_name": dataset_name,
        "dataset_case_count": len(_CASES),
        "run_target": os.getenv("EVAL_TRIGGER_COMPONENT", "unified_expense_assistant"),
        "requested_models": [
            name.strip()
            for name in os.getenv("EVAL_TRIGGER_MODELS", ",".join(_MODEL_NAMES)).split(",")
            if name.strip()
        ],
        "requested_datasets": requested_datasets,
        "total_cases": len(_CASES),
        "models": _MODEL_NAMES,
        "by_model": by_model,
        "by_suite": by_suite,
        "results": _RESULTS,
    }
    _OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _post_eval_run(payload, dataset_name)

    lines = [
        "",
        "═" * 64,
        "  CHATBOT EVAL MODEL MATRIX",
        "═" * 64,
    ]
    for model, metrics in by_model.items():
        lines.append(
            f"  {model}: {metrics['passed_cases']}/{metrics['total_cases']} "
            f"({metrics['pass_rate']:.0%}) · avg_tool_calls={metrics['avg_tool_calls']:.2f} "
            f"· avg_latency={metrics['avg_latency_ms']:.0f}ms · est_cost=${metrics['total_cost_estimate_usd']:.6f}"
        )
    lines.append(f"  snapshot: {_OUT_PATH}")
    lines.append("═" * 64)
    sys.stderr.write("\n".join(lines) + "\n")


def _post_eval_run(matrix_payload: dict, dataset_name: str) -> None:
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r.get("passed"))
    run_payload = {
        "started_at": matrix_payload["started_at"],
        "finished_at": matrix_payload["finished_at"],
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "results": _RESULTS,
        "trigger": "pytest",
        "metadata": {
            "component": matrix_payload["run_target"],
            "model": ",".join(_MODEL_NAMES),
            "models": _MODEL_NAMES,
            "dataset": dataset_name,
            "dataset_name": dataset_name,
            "dataset_file": str(_DATASET_PATH.name),
            "dataset_case_count": len(_CASES),
            "requested_datasets": matrix_payload["requested_datasets"],
            "prompt_version": os.getenv("EVAL_PROMPT_VERSION", "v1"),
        },
    }
    api_url = os.environ.get("EVAL_API_URL", "http://localhost:8000/api/eval/runs")
    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(run_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            sys.stderr.write(f"  ✓ Chatbot eval run posted to Observatory ({resp.status})\n")
    except (urllib.error.URLError, OSError, TimeoutError):
        out_path = Path(tempfile.gettempdir()) / "chatbot_eval_last_run.json"
        out_path.write_text(json.dumps(run_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        sys.stderr.write(f"  ⚠ Observatory API unreachable. Chatbot eval run saved to: {out_path}\n")


def _aggregate_rows(rows: list[dict]) -> dict:
    total = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    return {
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "avg_tool_calls": round(sum(r.get("tool_call_count", 0) for r in rows) / total, 4) if total else 0.0,
        "avg_latency_ms": round(sum(r.get("latency_ms", 0) for r in rows) / total, 2) if total else 0.0,
        "total_cost_estimate_usd": round(sum(r.get("cost_estimate_usd", 0.0) for r in rows), 8),
    }
