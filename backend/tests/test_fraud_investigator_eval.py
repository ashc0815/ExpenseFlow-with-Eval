"""Eval — fraud investigator agent verdict vs human labels.

Closes the eval loop on PR-A → PR-B → PR-C: with the OODA agent in
place, this test asks "does the agent's verdict actually match what a
human would say?" via Cohen's κ on the 3-class verdict (clean /
suspicious / fraud).

Two assertions per case:
  1. Verdict equality contributes to the κ aggregation.
  2. `must_call_tools` from the human label MUST be a subset of
     `tools_called` from the agent — i.e. the agent didn't skip
     evidence the labeler thinks was essential.

Force-mock-only: the test pins `force_mock=True` so behavior is
deterministic across CI runs. The real-LLM path is exercised by the
unit tests in test_fraud_investigator.py with scripted clients.

Snapshot output (consumed by the dashboard Review-Quality tab):
  backend/tests/eval_judge_fraud_investigator_latest.json
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("EVAL_DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", tempfile.gettempdir())

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.fraud_investigator import investigate_submission
from backend.db.store import (
    Base, EvalBase, create_submission, upsert_employee,
)
from backend.tests.test_judge_agreement import (
    cohens_kappa, confusion_matrix, kappa_band,
)


_DATASET_PATH = (
    Path(__file__).resolve().parent
    / "eval_datasets"
    / "eval_regression_fraud_investigator.yaml"
)
_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "eval_judge_fraud_investigator_latest.json"
)
_VERDICT_CLASSES = ("clean", "suspicious", "fraud")
_THRESHOLD_KAPPA = 0.40   # same scale as B1


_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def setup_module(_):
    import backend.config as _cfg
    _cfg.DATABASE_URL = _DB_URL

    async def _init():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(EvalBase.metadata.create_all)

    asyncio.new_event_loop().run_until_complete(_init())


def teardown_module(_):
    try:
        asyncio.new_event_loop().run_until_complete(_engine.dispose())
    except Exception:
        pass
    try:
        os.unlink(_TMP_DB.name)
    except PermissionError:
        pass


# ── Helpers ──────────────────────────────────────────────────────────


def _load_cases() -> list[dict]:
    if not _DATASET_PATH.exists():
        return []
    return yaml.safe_load(_DATASET_PATH.read_text(encoding="utf-8")) or []


def _is_placeholder(case: dict) -> bool:
    return (
        "_placeholder_" in str(case.get("id", ""))
        or str(case.get("labeler_note", "")).strip().upper() == "PLACEHOLDER"
    )


def _strip_placeholders(cases: list[dict]) -> tuple[list[dict], int]:
    real = [c for c in cases if not _is_placeholder(c)]
    return real, len(cases) - len(real)


async def _seed_case_context(case: dict) -> None:
    """Seed the per-case context. Each case uses unique employee_ids
    so no per-test teardown is needed."""
    ctx = case.get("context") or {}

    emp = ctx.get("employee")
    if emp:
        async with _Session() as db:
            await upsert_employee(db, {
                "id": emp["id"],
                "name": emp.get("name", emp["id"]),
                "department": emp.get("department", "Engineering"),
                "cost_center": emp.get("cost_center", "ENG"),
                "level": emp.get("level", "L3"),
                "city": emp.get("city", "上海"),
            })

    history = ctx.get("history") or []
    if history and emp:
        async with _Session() as db:
            for i, h in enumerate(history):
                await create_submission(db, {
                    "id": f"hist_{case['id']}_{i}",
                    "employee_id": emp["id"],
                    "status": h.get("status", "finance_approved"),
                    "amount": Decimal(str(h["amount"])),
                    "currency": h.get("currency", "CNY"),
                    "category": h["category"],
                    "date": str(h["date"]),
                    "merchant": h.get("merchant", "Test"),
                    "receipt_url": "/uploads/test/x.jpg",
                    "cost_center": emp.get("cost_center", "ENG"),
                })

    peer_history = ctx.get("peer_history") or []
    for i, p in enumerate(peer_history):
        async with _Session() as db:
            # Make sure peer employee exists
            await upsert_employee(db, {
                "id": p["employee_id"],
                "name": p.get("name", p["employee_id"]),
                "department": p.get("department", "Engineering"),
                "cost_center": p.get("cost_center", emp.get("cost_center", "ENG") if emp else "ENG"),
                "level": p.get("level", "L3"),
            })
            await create_submission(db, {
                "id": f"peer_{case['id']}_{i}",
                "employee_id": p["employee_id"],
                "status": "finance_approved",
                "amount": Decimal(str(p["amount"])),
                "currency": p.get("currency", "CNY"),
                "category": p["category"],
                "date": str(p["date"]),
                "merchant": p.get("merchant", "Test"),
                "receipt_url": "/uploads/test/y.jpg",
                "cost_center": p.get("cost_center", emp.get("cost_center", "ENG") if emp else "ENG"),
            })


def _write_snapshot(payload: dict) -> None:
    _SNAPSHOT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── The eval test ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fraud_investigator_verdict_kappa():
    """Run all real labeled cases through the investigator (mock path)
    and compute Cohen's κ on the verdict against human labels."""
    raw = _load_cases()
    cases, n_placeholder = _strip_placeholders(raw)
    if not cases:
        _write_snapshot({
            "empty": True,
            "placeholder_count": n_placeholder,
            "message": (
                f"{n_placeholder} placeholder(s); replace with real "
                "labels in eval_regression_fraud_investigator.yaml"
            ),
        })
        pytest.skip(
            f"eval_regression_fraud_investigator.yaml has only "
            f"{n_placeholder} placeholder(s). Add real cases first."
        )

    human_verdicts: list[str] = []
    agent_verdicts: list[str] = []
    per_case: list[dict] = []
    must_call_failures: list[str] = []

    for case in cases:
        case_id = case["id"]
        await _seed_case_context(case)
        sub = case["submission"]
        fraud_signals = case.get("fraud_signals") or []
        risk_score = float(case.get("risk_score", 80))
        human_label = case.get("human_label") or {}
        expected_verdict = human_label.get("expected_verdict")
        if expected_verdict not in _VERDICT_CLASSES:
            per_case.append({
                "id": case_id,
                "error": f"unrecognized expected_verdict {expected_verdict!r}",
            })
            continue

        async with _Session() as db:
            try:
                result = await investigate_submission(
                    db,
                    submission=sub,
                    fraud_signals=fraud_signals,
                    risk_score=risk_score,
                    force_mock=True,   # determinism
                )
            except Exception as exc:  # noqa: BLE001
                per_case.append({
                    "id": case_id,
                    "error": f"investigator threw: {exc}",
                    "human": expected_verdict,
                })
                continue

        agent_verdict = result["verdict"]
        human_verdicts.append(expected_verdict)
        agent_verdicts.append(agent_verdict)

        # must_call_tools subset assertion (collected, asserted at end)
        must_call = human_label.get("must_call_tools") or []
        missing_tools = [t for t in must_call if t not in result["tools_called"]]
        if missing_tools:
            must_call_failures.append(
                f"  [{case_id}] missing tools: {missing_tools} "
                f"(agent called {result['tools_called']})"
            )

        per_case.append({
            "id": case_id,
            "human": expected_verdict,
            "agent": agent_verdict,
            "agree": expected_verdict == agent_verdict,
            "agent_confidence": result.get("confidence"),
            "tools_called": result.get("tools_called", []),
            "rounds_used": result.get("rounds_used"),
        })

    kappa = cohens_kappa(human_verdicts, agent_verdicts)
    matrix = confusion_matrix(human_verdicts, agent_verdicts, _VERDICT_CLASSES)
    band = kappa_band(kappa)
    sample_n = len(human_verdicts)

    _write_snapshot({
        "component": "fraud_investigator",
        "metric": "cohens_kappa",
        "kappa": kappa,
        "band": band,
        "threshold": _THRESHOLD_KAPPA,
        "sample_size": sample_n,
        "classes": list(_VERDICT_CLASSES),
        "confusion_matrix": matrix,
        "per_case": per_case,
        "skipped_placeholders": n_placeholder,
        "must_call_failures": must_call_failures,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })

    # Tool-coverage failures fail the test cleanly with a useful message
    if must_call_failures:
        pytest.fail(
            "must_call_tools constraint violated:\n"
            + "\n".join(must_call_failures)
        )

    assert sample_n >= 1, "no usable labeled cases survived"
    assert kappa >= _THRESHOLD_KAPPA, (
        f"fraud_investigator vs human verdict κ={kappa} ({band}) below "
        f"threshold {_THRESHOLD_KAPPA}.\n"
        f"Read disagreeing cases in {_SNAPSHOT_PATH} (per_case[].agree==False) "
        "before tuning the agent."
    )
