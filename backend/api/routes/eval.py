"""Eval Observatory API — browse eval runs, traces, and case results.

Endpoints:
  GET   /api/eval/runs              List eval runs (paginated)
  GET   /api/eval/runs/{id}         Single run detail
  POST  /api/eval/runs              Record a new eval run
  GET   /api/eval/traces            List LLM traces (filterable; supports
                                    reviewed=true|false + failure_mode_tag)
  GET   /api/eval/traces/{id}       Single trace detail
  PATCH /api/eval/traces/{id}/review
                                    Mark a trace as reviewed by a human
                                    (Hamel "always be looking at data")
  GET   /api/eval/saturation        Per-component review stats: total /
                                    reviewed / unreviewed / failure-mode
                                    breakdown. Hamel saturation guideline.
  GET   /api/eval/agent-case-reviews
                                    Agent eval cases that should be manually
                                    reviewed (failed / FLAG / REJECT /
                                    guardrail issues)
  PATCH /api/eval/agent-case-reviews/{review_key}
                                    Mark an agent eval case as reviewed
  GET   /api/eval/stats             Aggregate stats (pass rate trend, component breakdown)
  GET   /api/eval/config            Read current eval config (6 factors)
  PUT   /api/eval/config            Update eval config
  GET   /api/eval/runs/{id1}/diff/{id2}  Compare metadata between two runs
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.store import (
    EvalRun, LLMTrace, Submission, get_db, get_eval_db,
    mark_trace_reviewed, saturation_summary,
)

router = APIRouter()


class TraceReviewBody(BaseModel):
    """PATCH /traces/{id}/review payload.

    failure_mode_tag semantics:
      "" or None → reviewed and judged correct
      non-empty   → reviewed and labeled with this failure-mode tag
                    (e.g. "wrong_attribution", "style_only", "false_positive")
    """
    reviewed_by: str
    failure_mode_tag: Optional[str] = None
    notes: Optional[str] = None


class AgentCaseReviewBody(BaseModel):
    """PATCH /agent-case-reviews/{review_key} payload."""
    reviewed_by: str
    failure_mode_tag: Optional[str] = None
    notes: Optional[str] = None

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_config.json"
_PROMPTS_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_prompts.json"
_HUMAN_FRAUD_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_human_fraud_latest.json"
_HUMAN_AMBIG_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_human_ambiguity_latest.json"
_CHATBOT_MATRIX_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_chatbot_model_matrix_latest.json"
_CHATBOT_CASES_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_datasets" / "chatbot_expense_assistant.yaml"
_CHATBOT_DATASET_SETS_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_datasets" / "chatbot_dataset_sets.yaml"
_AGENT_CASE_REVIEWS_PATH = Path(__file__).resolve().parents[2] / "tests" / "eval_agent_case_reviews.json"

# B1 (judge agreement) snapshot paths — written by test_judge_agreement.py.
# Map a logical "component" name (the same value the saturation endpoint
# accepts) to the on-disk snapshot file. Components without a snapshot
# fall through to an `empty: true` response so the dashboard can show a
# "no judge eval yet" placeholder instead of breaking.
_JUDGE_SNAPSHOTS: dict[str, Path] = {
    "ambiguity_detector": (
        Path(__file__).resolve().parents[2] / "tests" / "eval_judge_ambiguity_latest.json"
    ),
    "fraud_llm_analyzer": (
        Path(__file__).resolve().parents[2] / "tests" / "eval_judge_fraud_overall_risk_latest.json"
    ),
    "fraud_investigator": (
        Path(__file__).resolve().parents[2] / "tests" / "eval_judge_fraud_investigator_latest.json"
    ),
}


# ── Eval Runs ────────────────────────────────────────────────────────

@router.get("/runs")
async def list_eval_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_eval_db),
) -> dict:
    q = select(EvalRun).order_by(desc(EvalRun.started_at))
    total = (await db.execute(select(func.count()).select_from(EvalRun))).scalar_one()
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {
        "items": [_run_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/runs/{run_id}")
async def get_eval_run(run_id: str, db: AsyncSession = Depends(get_eval_db)) -> dict:
    result = await db.execute(select(EvalRun).where(EvalRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        return {"error": "not found"}
    return _run_to_dict(run)


@router.post("/runs")
async def create_eval_run(body: dict, db: AsyncSession = Depends(get_eval_db)) -> dict:
    """Record an eval run (called by the harness after completion)."""
    import uuid
    run = EvalRun(
        id=str(uuid.uuid4()),
        started_at=datetime.fromisoformat(body["started_at"]) if "started_at" in body else datetime.now(timezone.utc),
        finished_at=datetime.fromisoformat(body["finished_at"]) if "finished_at" in body else datetime.now(timezone.utc),
        total_cases=body.get("total_cases", 0),
        passed_cases=body.get("passed_cases", 0),
        pass_rate=body.get("pass_rate", 0.0),
        results=body.get("results"),
        trigger=body.get("trigger", "manual"),
        run_metadata=body.get("metadata"),
        component_metrics=body.get("component_metrics"),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return _run_to_dict(run)


# ── LLM Traces ───────────────────────────────────────────────────────

@router.get("/traces")
async def list_traces(
    component: Optional[str] = None,
    submission_id: Optional[str] = None,
    has_error: Optional[bool] = None,
    reviewed: Optional[bool] = None,
    failure_mode_tag: Optional[str] = None,
    sort: str = Query("created_at", pattern="^(created_at|latency_ms|component)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_eval_db),
) -> dict:
    q = select(LLMTrace)

    if component:
        # Accept comma-separated list so a single UI filter can match the
        # unified expense assistant and manager explain traces together.
        values = [v.strip() for v in component.split(",") if v.strip()]
        if len(values) == 1:
            q = q.where(LLMTrace.component == values[0])
        elif len(values) > 1:
            q = q.where(LLMTrace.component.in_(values))
    if submission_id:
        q = q.where(LLMTrace.submission_id == submission_id)
    if has_error is True:
        q = q.where(LLMTrace.error.isnot(None))
    elif has_error is False:
        q = q.where(LLMTrace.error.is_(None))
    # Review-state filters (Hamel "always be looking at data" workflow)
    if reviewed is True:
        q = q.where(LLMTrace.reviewed_at.is_not(None))
    elif reviewed is False:
        q = q.where(LLMTrace.reviewed_at.is_(None))
    if failure_mode_tag is not None:
        # `?failure_mode_tag=` (empty) → reviewed-and-correct
        # `?failure_mode_tag=wrong_attribution` → exact tag match
        q = q.where(LLMTrace.failure_mode_tag == failure_mode_tag)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    sort_col = getattr(LLMTrace, sort)
    q = q.order_by(desc(sort_col) if order == "desc" else sort_col)
    rows = (await db.execute(q.offset((page - 1) * page_size).limit(page_size))).scalars().all()

    return {
        "items": [_trace_to_dict(t) for t in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, db: AsyncSession = Depends(get_eval_db)) -> dict:
    result = await db.execute(select(LLMTrace).where(LLMTrace.id == trace_id))
    trace = result.scalar_one_or_none()
    if not trace:
        return {"error": "not found"}
    return _trace_to_dict(trace, include_prompt=True)


@router.patch("/traces/{trace_id}/review")
async def review_trace(
    trace_id: str,
    body: TraceReviewBody,
    db: AsyncSession = Depends(get_eval_db),
) -> dict:
    """Mark a trace as reviewed.

    Hamel: every reviewed trace either confirms the system was correct
    or names a specific failure mode. Without that, "we looked at it"
    has no signal.
    """
    trace = await mark_trace_reviewed(
        db, trace_id,
        reviewed_by=body.reviewed_by,
        failure_mode_tag=body.failure_mode_tag,
        notes=body.notes,
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return _trace_to_dict(trace)


@router.get("/saturation")
async def get_saturation(
    component: str = Query(..., description="component name to summarize"),
    db: AsyncSession = Depends(get_eval_db),
) -> dict:
    """Per-component review stats — Hamel saturation diagnostic.

    Returns total / reviewed / unreviewed / correct counts plus a
    {failure_mode_tag: count} breakdown. Saturation is reached when
    scrolling N more reviewed traces surfaces no new tag values.
    """
    return await saturation_summary(db, component=component)


# ── Stats ─────────────────────────────────────────────────────────────

@router.get("/stats")
async def eval_stats(db: AsyncSession = Depends(get_eval_db)) -> dict:
    """Aggregate stats: recent pass rates + component breakdown of traces."""
    # Recent 10 eval runs for trend
    runs = (await db.execute(
        select(EvalRun).order_by(desc(EvalRun.started_at)).limit(10)
    )).scalars().all()

    trend = [
        {"date": r.started_at.isoformat() if r.started_at else None, "pass_rate": r.pass_rate}
        for r in reversed(list(runs))
    ]

    # Trace count by component
    comp_counts = (await db.execute(
        select(LLMTrace.component, func.count(LLMTrace.id))
        .group_by(LLMTrace.component)
    )).all()

    # Error rate by component
    error_counts = (await db.execute(
        select(LLMTrace.component, func.count(LLMTrace.id))
        .where(LLMTrace.error.isnot(None))
        .group_by(LLMTrace.component)
    )).all()
    error_map = dict(error_counts)

    components = []
    for comp, count in comp_counts:
        errors = error_map.get(comp, 0)
        components.append({
            "component": comp,
            "total_traces": count,
            "error_count": errors,
            "error_rate": round(errors / count, 4) if count > 0 else 0,
        })

    return {"trend": trend, "components": components}


# ── Auto-approval funnel KPI ─────────────────────────────────────────
# Inspired by Airwallex Spend AI's published metric: "64% of expenses are
# auto-approved because the system already verified compliance in real time".
# This endpoint computes the same funnel for ExpenseFlow's own data so the
# eval dashboard can show whether AI tiering is actually doing useful work.
#
# Definitions (matches the tier_map in submissions._run_pipeline):
#   T1 / T2 → AI auto-approve (low / next-low risk)
#   T3      → human review needed
#   T4      → AI suggests reject
# auto_approve_rate = (T1 + T2) / (T1 + T2 + T3 + T4)
#
# Reads from the BUSINESS db (submissions table), not the eval db, so it's
# tracking real production-style outcomes — not eval-suite synthetic cases.

@router.get("/auto-approval-rate")
async def auto_approval_rate(
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=365, description="Look-back window in days"),
) -> dict:
    """Tier breakdown + auto-approval funnel for the recent N days.

    Returns:
      {
        window_days: 30,
        total: 142,
        by_tier: {T1: 78, T2: 24, T3: 30, T4: 10},
        auto_approve_count: 102,    # T1 + T2
        auto_approve_rate: 0.7183,  # 71.83%
        human_review_count: 30,
        human_review_rate: 0.2113,
        rejection_count: 10,
        rejection_rate: 0.0704,
      }

    A submission is counted only if it has been reviewed (tier IS NOT NULL).
    Open / draft / processing submissions are excluded.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (await db.execute(
        select(Submission.tier, func.count(Submission.id))
        .where(Submission.tier.isnot(None))
        .where(Submission.created_at >= cutoff)
        .group_by(Submission.tier)
    )).all()

    by_tier = {tier: count for tier, count in rows if tier}
    total = sum(by_tier.values())

    auto = by_tier.get("T1", 0) + by_tier.get("T2", 0)
    review = by_tier.get("T3", 0)
    reject = by_tier.get("T4", 0)

    def _rate(n: int) -> float:
        return round(n / total, 4) if total > 0 else 0.0

    return {
        "window_days": days,
        "total": total,
        "by_tier": {
            "T1": by_tier.get("T1", 0),
            "T2": by_tier.get("T2", 0),
            "T3": by_tier.get("T3", 0),
            "T4": by_tier.get("T4", 0),
        },
        "auto_approve_count":  auto,
        "auto_approve_rate":   _rate(auto),
        "human_review_count":  review,
        "human_review_rate":   _rate(review),
        "rejection_count":     reject,
        "rejection_rate":      _rate(reject),
    }


# ── Serializers ───────────────────────────────────────────────────────

def _run_to_dict(r: EvalRun) -> dict:
    return {
        "id": r.id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "total_cases": r.total_cases,
        "passed_cases": r.passed_cases,
        "pass_rate": r.pass_rate,
        "results": r.results,
        "trigger": r.trigger,
        "metadata": r.run_metadata,
        "component_metrics": r.component_metrics,
    }


# ── Eval Config ──────────────────────────────────────────────────────

@router.get("/config")
async def get_eval_config() -> dict:
    """Read current eval config (the 6 tunable factors)."""
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


@router.put("/config")
async def update_eval_config(body: dict) -> dict:
    """Update eval config. Merges with existing config."""
    existing = {}
    if _CONFIG_PATH.exists():
        existing = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    existing.update(body)
    _CONFIG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return existing


# ── Run Eval Trigger ────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_eval_running = False


def _load_chatbot_cases_for_dataset_registry() -> list[dict]:
    if not _CHATBOT_CASES_PATH.exists():
        return []
    return yaml.safe_load(_CHATBOT_CASES_PATH.read_text(encoding="utf-8")) or []


def _load_chatbot_case_definitions() -> dict[str, dict]:
    cases = _load_chatbot_cases_for_dataset_registry()
    return {
        str(case.get("id")): case
        for case in cases
        if isinstance(case, dict) and case.get("id")
    }


def _dataset_registry_case_count(cases: list[dict], spec: dict) -> int:
    suites = set(spec.get("suites") or [])
    scenarios = set(spec.get("scenarios") or [])
    case_ids = set(spec.get("case_ids") or [])
    tags = set(str(tag) for tag in (spec.get("tags") or []))

    def _matches(case: dict) -> bool:
        case_tags = set(str(tag) for tag in (case.get("tags") or []))
        return (
            case.get("suite") in suites
            or case.get("scenario") in scenarios
            or case.get("id") in case_ids
            or bool(case_tags & tags)
        )

    return sum(1 for case in cases if _matches(case))


@router.get("/chatbot/datasets")
async def list_chatbot_datasets() -> dict:
    """Return named unified-expense-assistant dataset sets for Run/Compare UI."""
    cases = _load_chatbot_cases_for_dataset_registry()
    if not _CHATBOT_DATASET_SETS_PATH.exists():
        return {"items": []}
    rows = yaml.safe_load(_CHATBOT_DATASET_SETS_PATH.read_text(encoding="utf-8")) or []
    items = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        item = dict(row)
        item["case_count"] = _dataset_registry_case_count(cases, row)
        items.append(item)
    return {"items": items}


@router.post("/trigger")
async def trigger_eval(body: dict = {}) -> dict:
    """Trigger an eval run via pytest subprocess.

    Body (optional):
      component: "fraud" | "ambiguity" | "deterministic" | "unified_expense_assistant" | "all"
      models: ["OpenAI-4o-mini", ...]      # unified assistant model-matrix runner
      datasets: ["all_scenarios1", ...]    # unified assistant dataset-set filters

    Returns immediately with status; results appear in /runs after completion.
    """
    global _eval_running
    if _eval_running:
        return {"status": "already_running"}

    component = body.get("component", "deterministic")
    requested_models = [
        str(m).strip()
        for m in (body.get("models") or [])
        if str(m).strip()
    ]
    requested_datasets = [
        str(d).strip()
        for d in (body.get("datasets") or [])
        if str(d).strip()
    ]

    if component in ("chat", "chatbot", "expense_assistant", "unified_expense_assistant"):
        cmd = [
            sys.executable, "-m", "pytest",
            "backend/tests/test_chatbot_eval.py",
            "-q", "--tb=short",
        ]
        k_filter = ""
    else:
        # Map component to pytest -k filter
        k_filter = {
            "fraud": "deterministic or layer or classifier",
            "fraud_llm": "llm",
            "ambiguity": "ambiguity",
            "deterministic": "deterministic or layer or classifier",
            "all": "",
        }.get(component, "deterministic or layer or classifier")

        cmd = [
            sys.executable, "-m", "pytest",
            "backend/tests/test_eval_harness.py",
            "-q", "--tb=short",
        ]
        if k_filter:
            cmd += ["-k", k_filter]

    _eval_running = True

    async def _run():
        global _eval_running
        try:
            env = {
                **__import__("os").environ,
                "PYTHONPATH": str(_PROJECT_ROOT),
                "EVAL_TRIGGER_COMPONENT": str(component),
                "EVAL_TRIGGER_MODELS": ",".join(requested_models),
                "EVAL_TRIGGER_DATASETS": ",".join(requested_datasets),
            }
            if component in ("chat", "chatbot", "expense_assistant", "unified_expense_assistant"):
                env["CHATBOT_EVAL_ALLOW_FAILURES"] = "1"
            if requested_models and component in ("chat", "chatbot", "expense_assistant", "unified_expense_assistant"):
                env["CHATBOT_EVAL_MODELS"] = ",".join(requested_models)
            if requested_datasets and component in ("chat", "chatbot", "expense_assistant", "unified_expense_assistant"):
                env["CHATBOT_EVAL_DATASETS"] = ",".join(requested_datasets)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(_PROJECT_ROOT),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode(errors="replace")[-2000:],
                "stderr": stderr.decode(errors="replace")[-2000:],
            }
        finally:
            _eval_running = False

    # Run in background — results post to /runs via harness teardown
    asyncio.create_task(_run())
    return {
        "status": "started",
        "component": component,
        "k_filter": k_filter,
        "models": requested_models,
        "datasets": requested_datasets,
    }


@router.get("/trigger/status")
async def trigger_status() -> dict:
    """Check if an eval run is currently in progress."""
    return {"running": _eval_running}


# ── Run Diff ─────────────────────────────────────────────────────────

@router.get("/runs/{run_id_a}/diff/{run_id_b}")
async def diff_runs(run_id_a: str, run_id_b: str, db: AsyncSession = Depends(get_eval_db)) -> dict:
    """Compare metadata and results between two eval runs."""
    ra = (await db.execute(select(EvalRun).where(EvalRun.id == run_id_a))).scalar_one_or_none()
    rb = (await db.execute(select(EvalRun).where(EvalRun.id == run_id_b))).scalar_one_or_none()
    if not ra or not rb:
        return {"error": "one or both runs not found"}

    meta_a = ra.run_metadata or {}
    meta_b = rb.run_metadata or {}

    # Find changed metadata fields
    all_keys = set(list(meta_a.keys()) + list(meta_b.keys()))
    meta_diff = {}
    for k in sorted(all_keys):
        va, vb = meta_a.get(k), meta_b.get(k)
        if va != vb:
            meta_diff[k] = {"a": va, "b": vb}

    # Find case result changes (PASS↔FAIL)
    cases_a = {c["case_id"]: c["passed"] for c in (ra.results or [])}
    cases_b = {c["case_id"]: c["passed"] for c in (rb.results or [])}
    all_cases = set(list(cases_a.keys()) + list(cases_b.keys()))
    case_diff = []
    for cid in sorted(all_cases):
        pa, pb = cases_a.get(cid), cases_b.get(cid)
        if pa != pb:
            case_diff.append({"case_id": cid, "a": pa, "b": pb})

    return {
        "run_a": {
            "id": ra.id,
            "pass_rate": ra.pass_rate,
            "total": ra.total_cases,
            "started_at": ra.started_at.isoformat() if ra.started_at else None,
            "metadata": meta_a,
        },
        "run_b": {
            "id": rb.id,
            "pass_rate": rb.pass_rate,
            "total": rb.total_cases,
            "started_at": rb.started_at.isoformat() if rb.started_at else None,
            "metadata": meta_b,
        },
        "metadata_diff": meta_diff,
        "case_diff": case_diff,
        "summary": {
            "metadata_changes": len(meta_diff),
            "regressions": sum(1 for c in case_diff if c.get("a") is True and c.get("b") is False),
            "improvements": sum(1 for c in case_diff if c.get("a") is False and c.get("b") is True),
        },
    }


# ── Prompt Management ────────────────────────────────────────────────

def _load_prompts() -> dict:
    if _PROMPTS_PATH.exists():
        return json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))
    return {"prompts": {}}


def _save_prompts(data: dict) -> None:
    _PROMPTS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@router.get("/prompts")
async def list_prompts() -> dict:
    """List all prompt templates with their versions."""
    data = _load_prompts()
    # Return summary (without full content for list view)
    summary = {}
    for key, p in data.get("prompts", {}).items():
        versions = p.get("versions", {})
        summary[key] = {
            "name": p.get("name"),
            "component": p.get("component"),
            "description": p.get("description"),
            "active_version": p.get("active_version"),
            "version_count": len(versions),
            "version_list": sorted(versions.keys()),
        }
    return {"prompts": summary}


@router.get("/prompts/{prompt_key}")
async def get_prompt(prompt_key: str) -> dict:
    """Get a prompt template with all its versions (full content)."""
    data = _load_prompts()
    prompt = data.get("prompts", {}).get(prompt_key)
    if not prompt:
        return {"error": "prompt not found"}
    return {"key": prompt_key, **prompt}


@router.get("/prompts/{prompt_key}/versions/{version}")
async def get_prompt_version(prompt_key: str, version: str) -> dict:
    """Get a specific prompt version's content."""
    data = _load_prompts()
    prompt = data.get("prompts", {}).get(prompt_key)
    if not prompt:
        return {"error": "prompt not found"}
    ver = prompt.get("versions", {}).get(version)
    if not ver:
        return {"error": "version not found"}
    return {"key": prompt_key, "version": version, **ver}


@router.put("/prompts/{prompt_key}/versions/{version}")
async def save_prompt_version(prompt_key: str, version: str, body: dict) -> dict:
    """Create or update a prompt version. Body: {content, notes}."""
    data = _load_prompts()
    prompts = data.setdefault("prompts", {})

    if prompt_key not in prompts:
        return {"error": "prompt key not found — use an existing key"}

    versions = prompts[prompt_key].setdefault("versions", {})
    versions[version] = {
        "content": body.get("content", ""),
        "notes": body.get("notes", ""),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    _save_prompts(data)
    return {"key": prompt_key, "version": version, "saved": True}


@router.put("/prompts/{prompt_key}/active")
async def set_active_version(prompt_key: str, body: dict) -> dict:
    """Set the active prompt version. Body: {version: "v2"}."""
    data = _load_prompts()
    prompt = data.get("prompts", {}).get(prompt_key)
    if not prompt:
        return {"error": "prompt not found"}
    version = body.get("version")
    if version not in prompt.get("versions", {}):
        return {"error": f"version '{version}' does not exist"}
    prompt["active_version"] = version
    _save_prompts(data)
    return {"key": prompt_key, "active_version": version}


# ── Human-Labeled Evals (fraud subfield matrix + ambiguity confusion) ──

@router.get("/human/fraud")
async def get_human_fraud_eval() -> dict:
    """Return the latest fraud analyzer human-labeled eval results.

    File is written by pytest backend/tests/test_human_eval.py::test_fraud_human_eval.
    Returns {empty: true} if the file does not exist yet (before first run).
    """
    if not _HUMAN_FRAUD_PATH.exists():
        return {"empty": True, "message": "No fraud human-eval run yet. Run: pytest backend/tests/test_human_eval.py"}
    try:
        return json.loads(_HUMAN_FRAUD_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"empty": True, "error": str(exc)}


@router.get("/judge-agreement/{component}")
async def get_judge_agreement(component: str) -> dict:
    """Return the latest judge-agreement (Cohen's κ) snapshot for a component.

    Snapshot file is written by pytest backend/tests/test_judge_agreement.py.
    Returns {empty: true} when the component has no snapshot file or no
    real labeled cases yet.

    The dashboard's Review Quality tab consumes this to show the κ value,
    band (poor/fair/moderate/substantial/almost_perfect), confusion matrix,
    and per-case agreement rows.
    """
    snapshot_path = _JUDGE_SNAPSHOTS.get(component)
    if snapshot_path is None:
        return {
            "empty": True,
            "message": f"no judge-agreement snapshot configured for component '{component}'",
        }
    if not snapshot_path.exists():
        return {
            "empty": True,
            "message": (
                f"No judge-agreement run yet for {component}. "
                "Run: pytest backend/tests/test_judge_agreement.py"
            ),
        }
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"empty": True, "error": str(exc)}


@router.get("/human/ambiguity")
async def get_human_ambiguity_eval() -> dict:
    """Return the latest ambiguity detector human-labeled eval results.

    File is written by pytest backend/tests/test_human_eval.py::test_ambiguity_human_eval.
    """
    if not _HUMAN_AMBIG_PATH.exists():
        return {"empty": True, "message": "No ambiguity human-eval run yet. Run: pytest backend/tests/test_human_eval.py"}
    try:
        return json.loads(_HUMAN_AMBIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"empty": True, "error": str(exc)}


def _load_agent_case_reviews() -> dict:
    if not _AGENT_CASE_REVIEWS_PATH.exists():
        return {"reviews": {}}
    try:
        data = json.loads(_AGENT_CASE_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"reviews": {}}
    if not isinstance(data, dict):
        return {"reviews": {}}
    data.setdefault("reviews", {})
    return data


def _save_agent_case_reviews(data: dict) -> None:
    data.setdefault("reviews", {})
    _AGENT_CASE_REVIEWS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _agent_case_review_reasons(case: dict) -> list[str]:
    reasons: list[str] = []
    actual = case.get("decision_label_actual")
    expected = case.get("decision_label_expected")

    if case.get("passed") is False:
        reasons.append("eval_failed")
    if actual == "FLAG_FOR_HUMAN":
        reasons.append("flag_for_human")
    if actual == "REJECT":
        reasons.append("reject")
    if expected and actual != expected:
        reasons.append("decision_mismatch")

    for grader in case.get("graders") or []:
        if grader.get("passed") is False:
            name = grader.get("name") or "grader"
            reasons.append(f"grader_failed:{name}")

    return reasons


def _agent_case_review_priority(case: dict, reasons: list[str]) -> str:
    joined = " ".join(reasons)
    if (
        "eval_failed" in reasons
        or "reject" in reasons
        or "decision_mismatch" in reasons
        or "forbidden" in joined
        or "unsafe" in joined
        or "trace_shape" in joined
    ):
        return "high"
    if "flag_for_human" in reasons:
        return "medium"
    return "low"


def _agent_case_review_item(
    case: dict,
    *,
    run_started_at: Optional[str],
    run_finished_at: Optional[str],
    review: Optional[dict],
    case_def: Optional[dict] = None,
) -> dict:
    reasons = _agent_case_review_reasons(case)
    model = case.get("model") or "unknown-model"
    case_id = case.get("case_id") or "unknown-case"
    review_key = f"{model}::{case_id}"
    failed_graders = [
        g.get("name") or "grader"
        for g in (case.get("graders") or [])
        if g.get("passed") is False
    ]
    return {
        "review_key": review_key,
        "case_id": case_id,
        "model": model,
        "suite": case.get("suite"),
        "scenario": case.get("scenario"),
        "difficulty": case.get("difficulty"),
        "tags": case.get("tags") or [],
        "passed": case.get("passed"),
        "decision_label_actual": case.get("decision_label_actual"),
        "decision_label_expected": case.get("decision_label_expected"),
        "subagents": case.get("subagents") or [],
        "tool_calls": case.get("tool_calls") or [],
        "tool_call_count": len(case.get("tool_calls") or []),
        "agent_trace_steps": case.get("agent_trace_steps") or [],
        "assistant_text": case.get("assistant_text"),
        "draft_fields": case.get("draft_fields") or {},
        "field_sources": case.get("field_sources") or {},
        "messages": (case_def or {}).get("messages") or [],
        "scripted_turns": (case_def or {}).get("scripted_turns") or [],
        "expect": (case_def or {}).get("expect") or {},
        "expected_final_text": (case_def or {}).get("final_text"),
        "graders": case.get("graders") or [],
        "failed_graders": failed_graders,
        "review_reasons": reasons,
        "review_priority": _agent_case_review_priority(case, reasons),
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "reviewed_at": (review or {}).get("reviewed_at"),
        "reviewed_by": (review or {}).get("reviewed_by"),
        "failure_mode_tag": (review or {}).get("failure_mode_tag"),
        "review_notes": (review or {}).get("notes"),
    }


@router.get("/agent-case-reviews")
async def list_agent_case_reviews(
    reviewed: Optional[bool] = None,
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    """Return Agent Trace cases that should enter human review.

    This intentionally reads from the same snapshot as the Agent Trace tab, so
    Review Quality is connected to the actual agent eval cases instead of only
    low-level LLM call logs.
    """
    if not _CHATBOT_MATRIX_PATH.exists():
        return {
            "empty": True,
            "message": (
                "No unified expense assistant model-matrix run yet. Run: "
                "pytest backend/tests/test_chatbot_eval.py -q"
            ),
        }
    try:
        matrix = json.loads(_CHATBOT_MATRIX_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"empty": True, "error": str(exc)}

    reviews = _load_agent_case_reviews().get("reviews", {})
    case_defs = _load_chatbot_case_definitions()
    items: list[dict] = []
    for case in matrix.get("results") or []:
        reasons = _agent_case_review_reasons(case)
        if not reasons:
            continue
        model = case.get("model") or "unknown-model"
        case_id = case.get("case_id") or "unknown-case"
        review_key = f"{model}::{case_id}"
        review = reviews.get(review_key)
        is_reviewed = review is not None and review.get("reviewed_at") is not None
        if reviewed is True and not is_reviewed:
            continue
        if reviewed is False and is_reviewed:
            continue
        items.append(
            _agent_case_review_item(
                case,
                run_started_at=matrix.get("started_at"),
                run_finished_at=matrix.get("finished_at"),
                review=review,
                case_def=case_defs.get(case_id),
            )
        )

    priority_order = {"high": 0, "medium": 1, "low": 2}
    items.sort(
        key=lambda item: (
            priority_order.get(item["review_priority"], 9),
            item.get("suite") or "",
            item.get("case_id") or "",
        )
    )

    all_review_items = [
        _agent_case_review_item(
            case,
            run_started_at=matrix.get("started_at"),
            run_finished_at=matrix.get("finished_at"),
            review=reviews.get(f"{case.get('model') or 'unknown-model'}::{case.get('case_id') or 'unknown-case'}"),
            case_def=case_defs.get(str(case.get("case_id") or "")),
        )
        for case in (matrix.get("results") or [])
        if _agent_case_review_reasons(case)
    ]
    reviewed_count = sum(1 for item in all_review_items if item.get("reviewed_at"))
    correct_count = sum(
        1
        for item in all_review_items
        if item.get("reviewed_at") and not item.get("failure_mode_tag")
    )
    by_failure_mode: dict[str, int] = {}
    for item in all_review_items:
        tag = item.get("failure_mode_tag")
        if item.get("reviewed_at") and tag:
            by_failure_mode[tag] = by_failure_mode.get(tag, 0) + 1

    return {
        "source": str(_CHATBOT_MATRIX_PATH),
        "started_at": matrix.get("started_at"),
        "finished_at": matrix.get("finished_at"),
        "total_cases": matrix.get("total_cases"),
        "candidate_count": len(all_review_items),
        "reviewed": reviewed_count,
        "unreviewed": len(all_review_items) - reviewed_count,
        "correct": correct_count,
        "by_failure_mode": by_failure_mode,
        "items": items[:page_size],
        "page_size": page_size,
    }


@router.patch("/agent-case-reviews/{review_key:path}")
async def review_agent_case(review_key: str, body: AgentCaseReviewBody) -> dict:
    data = _load_agent_case_reviews()
    reviews = data.setdefault("reviews", {})
    existing = reviews.get(review_key, {})
    existing.update(
        {
            "review_key": review_key,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_by": body.reviewed_by,
            "failure_mode_tag": body.failure_mode_tag or "",
            "notes": body.notes,
        }
    )
    reviews[review_key] = existing
    _save_agent_case_reviews(data)
    return existing


@router.get("/chatbot/model-matrix")
async def get_chatbot_model_matrix() -> dict:
    """Return the latest unified expense-assistant model matrix snapshot.

    File is written by pytest backend/tests/test_chatbot_eval.py.
    """
    if not _CHATBOT_MATRIX_PATH.exists():
        return {
            "empty": True,
            "message": (
                "No unified expense assistant model-matrix run yet. Run: "
                "pytest backend/tests/test_chatbot_eval.py -q"
            ),
        }
    try:
        matrix = json.loads(_CHATBOT_MATRIX_PATH.read_text(encoding="utf-8"))
        case_defs = _load_chatbot_case_definitions()
        for result in matrix.get("results") or []:
            case_id = str(result.get("case_id") or "")
            case_def = case_defs.get(case_id) or {}
            result.setdefault("messages", case_def.get("messages") or [])
            result.setdefault("expect", case_def.get("expect") or {})
            result.setdefault("scripted_turns", case_def.get("scripted_turns") or [])
            result.setdefault("expected_final_text", case_def.get("final_text"))
        return matrix
    except Exception as exc:  # noqa: BLE001
        return {"empty": True, "error": str(exc)}


# ── Serializers ───────────────────────────────────────────────────────

def _trace_to_dict(t: LLMTrace, include_prompt: bool = False) -> dict:
    d: dict = {
        "id": t.id,
        "component": t.component,
        "submission_id": t.submission_id,
        "model": t.model,
        "latency_ms": t.latency_ms,
        "token_usage": t.token_usage,
        "error": t.error,
        "parsed_output": t.parsed_output,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        # Review state — Hamel "always be looking at data" workflow
        "reviewed_at": t.reviewed_at.isoformat() if t.reviewed_at else None,
        "reviewed_by": t.reviewed_by,
        "failure_mode_tag": t.failure_mode_tag,
        "review_notes": t.review_notes,
    }
    if include_prompt:
        d["prompt"] = t.prompt
        d["response"] = t.response
    return d
