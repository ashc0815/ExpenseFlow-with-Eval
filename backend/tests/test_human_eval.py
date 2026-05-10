"""Human-labeled eval harness — compares AI output against hand-labeled ground truth.

Two evals:
  1. Fraud analyzer: per-subfield accuracy across 6 subfields, bucketed.
  2. Ambiguity detector: 3-class confusion matrix (auto_pass / human_review / suggest_reject).

Run:
  pytest backend/tests/test_human_eval.py -v
  pytest backend/tests/test_human_eval.py::test_fraud_human_eval -v
  pytest backend/tests/test_human_eval.py::test_ambiguity_human_eval -v

Output:
  Writes two JSON files next to this test file that the Dashboard reads:
    backend/tests/eval_human_fraud_latest.json
    backend/tests/eval_human_ambiguity_latest.json
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml


_TEST_DIR = Path(__file__).resolve().parent
_FRAUD_YAML = _TEST_DIR / "eval_datasets" / "eval_capability_fraud_llm.yaml"
_AMBIG_YAML = _TEST_DIR / "eval_datasets" / "eval_capability_ambiguity.yaml"
_FRAUD_OUT = _TEST_DIR / "eval_human_fraud_latest.json"
_AMBIG_OUT = _TEST_DIR / "eval_human_ambiguity_latest.json"


# ── Env setup (only if not already configured by a parent run) ────────

os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", tempfile.gettempdir())
if "DATABASE_URL" not in os.environ:
    _tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp_db.close()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp_db.name}"


def _load_yaml(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _json_default(o):
    """JSON encoder fallback for date/datetime objects from YAML."""
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────────────
# Fraud analyzer: bucket-based per-subfield scoring
# ─────────────────────────────────────────────────────────────────────

_FRAUD_SUBFIELDS = (
    "template_score_bucket",
    "contradiction_found",
    "extracted_person_count",
    "person_amount_reasonable",
    "vagueness_bucket",
    "overall_risk",
)


def _score_to_bucket(score: Any) -> str:
    """0-29 → low, 30-69 → medium, 70-100 → high. None/invalid → low."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "low"
    if s < 30:
        return "low"
    if s < 70:
        return "medium"
    return "high"


def _derive_overall_risk(ai: dict, human_count: int | None) -> str:
    """Heuristic overall_risk from the 6 subfields.

    fraud       : person_amount_reasonable=False OR vagueness=high+template=high
    suspicious  : template=high OR contradiction=True OR vagueness=high
    clean       : otherwise
    """
    template = _score_to_bucket(ai.get("template_score"))
    vagueness = _score_to_bucket(ai.get("vagueness_score"))
    reasonable = ai.get("person_amount_reasonable", True)
    contradiction = bool(ai.get("contradiction_found"))

    if reasonable is False:
        return "fraud"
    if template == "high" and vagueness == "high":
        return "fraud"
    if template == "high" or contradiction or vagueness == "high":
        return "suspicious"
    return "clean"


def _grade_fraud_case(ai: dict, human: dict) -> dict[str, dict]:
    """Return {subfield: {ai, human, passed}} for all 6 subfields."""
    grades: dict[str, dict] = {}

    ai_template = _score_to_bucket(ai.get("template_score"))
    grades["template_score_bucket"] = {
        "ai": ai_template,
        "human": human.get("template_score_bucket"),
        "passed": ai_template == human.get("template_score_bucket"),
    }

    ai_contra = bool(ai.get("contradiction_found"))
    grades["contradiction_found"] = {
        "ai": ai_contra,
        "human": bool(human.get("contradiction_found")),
        "passed": ai_contra == bool(human.get("contradiction_found")),
    }

    ai_count = ai.get("extracted_person_count")
    human_count = human.get("extracted_person_count")
    if ai_count is None and human_count is None:
        count_pass = True
    elif ai_count is None or human_count is None:
        count_pass = False
    else:
        count_pass = abs(int(ai_count) - int(human_count)) <= 1  # tolerate off-by-one
    grades["extracted_person_count"] = {
        "ai": ai_count,
        "human": human_count,
        "passed": count_pass,
    }

    ai_reasonable = bool(ai.get("person_amount_reasonable", True))
    grades["person_amount_reasonable"] = {
        "ai": ai_reasonable,
        "human": bool(human.get("person_amount_reasonable", True)),
        "passed": ai_reasonable == bool(human.get("person_amount_reasonable", True)),
    }

    ai_vague = _score_to_bucket(ai.get("vagueness_score"))
    grades["vagueness_bucket"] = {
        "ai": ai_vague,
        "human": human.get("vagueness_bucket"),
        "passed": ai_vague == human.get("vagueness_bucket"),
    }

    derived = _derive_overall_risk(ai, human_count)
    grades["overall_risk"] = {
        "ai": derived,
        "human": human.get("overall_risk"),
        "passed": derived == human.get("overall_risk"),
        "note": "derived from AI subfields, not a direct AI output",
    }

    return grades


def _make_submission_row(inp: dict):
    from backend.services.fraud_rules import SubmissionRow
    return SubmissionRow(
        id=f"hf-{inp.get('description', 'x')[:20]}",
        employee_id="human-eval-emp",
        description=inp.get("description", ""),
        category=inp.get("category", "meal"),
        amount=float(inp.get("amount", 0.0)),
        currency=inp.get("currency", "CNY"),
        merchant=inp.get("merchant", "unknown"),
        city=inp.get("city", "上海"),
        date=str(inp.get("date", date.today().isoformat())),
    )


def _is_placeholder(case: dict) -> bool:
    """Treat a case as a placeholder when its id starts with `*_placeholder_*`
    or its description starts with `PLACEHOLDER`. Placeholders carry no human
    judgment, so running them produces fake-pass numbers and erodes trust in
    the eval. Hamel: don't ship false-confidence eval signals."""
    return (
        "_placeholder_" in str(case.get("id", ""))
        or str(case.get("description", "")).startswith("PLACEHOLDER")
    )


def _strip_placeholders(cases: list[dict]) -> tuple[list[dict], int]:
    """Return (real_cases, placeholder_count)."""
    real = [c for c in cases if not _is_placeholder(c)]
    return real, len(cases) - len(real)


def test_fraud_human_eval() -> None:
    """Run fraud analyzer against all human-labeled cases.

    Pytest passes if at least one case runs (even if subfield accuracy is low).
    The actual quality signal is in eval_human_fraud_latest.json → Dashboard.

    Placeholder cases (id starts `fh_placeholder_*`) are dropped before the
    run — they carry no human judgment, and including them would produce
    fake-pass numbers.
    """
    raw_cases = _load_yaml(_FRAUD_YAML)
    cases, placeholder_count = _strip_placeholders(raw_cases)
    if not cases:
        _write_fraud_results(
            [], 0, 0,
            f"no real cases yet ({placeholder_count} placeholder(s); replace with real labels)"
        )
        pytest.skip(
            f"eval_capability_fraud_llm.yaml has {placeholder_count} placeholder(s) "
            "and no real cases — replace placeholders with real human-labeled "
            "examples before this eval is meaningful."
        )

    results: list[dict] = []
    use_real = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("AGENT_USE_REAL_LLM") == "1"

    for case in cases:
        case_id = case["id"]
        inp = case["input"]
        human = case["human_label"]

        if use_real:
            from backend.services.llm_fraud_analyzer import analyze_submission
            row = _make_submission_row(inp)
            recent = inp.get("recent_descriptions", [])
            loop = asyncio.new_event_loop()
            try:
                ai_output = loop.run_until_complete(
                    analyze_submission(row, recent, inp.get("receipt_location"))
                )
            finally:
                loop.close()
        else:
            # Mock output: give neutral scores so the Dashboard has something to show
            ai_output = {
                "template_score": 0,
                "contradiction_found": False,
                "extracted_person_count": human.get("extracted_person_count"),
                "per_person_amount": None,
                "person_amount_reasonable": True,
                "vagueness_score": 0,
            }

        grades = _grade_fraud_case(ai_output, human)
        subfield_passed = sum(1 for g in grades.values() if g["passed"])
        results.append({
            "case_id": case_id,
            "description": case.get("description", ""),
            "input": inp,
            "ai_output": ai_output,
            "human_label": human,
            "labeler_note": case.get("labeler_note", ""),
            "grades": grades,
            "subfield_passed": subfield_passed,
            "subfield_total": len(_FRAUD_SUBFIELDS),
            "all_passed": subfield_passed == len(_FRAUD_SUBFIELDS),
        })

    total_subfield_checks = sum(r["subfield_total"] for r in results)
    passed_subfield_checks = sum(r["subfield_passed"] for r in results)
    _write_fraud_results(results, passed_subfield_checks, total_subfield_checks,
                         "real_llm" if use_real else "mock_neutral")
    assert results  # at least one case ran


def _write_fraud_results(results: list[dict], passed: int, total: int, mode: str) -> None:
    per_subfield_acc: dict[str, dict] = {}
    for sf in _FRAUD_SUBFIELDS:
        pass_count = sum(1 for r in results if r["grades"].get(sf, {}).get("passed"))
        per_subfield_acc[sf] = {
            "passed": pass_count,
            "total": len(results),
            "accuracy": round(pass_count / len(results), 3) if results else 0.0,
        }
    payload = {
        "kind": "fraud_human_eval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,  # real_llm | mock_neutral
        "total_cases": len(results),
        "total_subfield_checks": total,
        "passed_subfield_checks": passed,
        "overall_accuracy": round(passed / total, 3) if total else 0.0,
        "per_subfield_accuracy": per_subfield_acc,
        "results": results,
    }
    _write_json(_FRAUD_OUT, payload)


# ─────────────────────────────────────────────────────────────────────
# Ambiguity detector: 3-class confusion matrix
# ─────────────────────────────────────────────────────────────────────

_AMBIG_CLASSES = ("auto_pass", "human_review", "suggest_reject")


def test_ambiguity_human_eval() -> None:
    """Run ambiguity detector against human gold recommendations.

    Placeholder cases (id starts `ah_placeholder_*`) are dropped before the
    run — see _is_placeholder above.
    """
    raw_cases = _load_yaml(_AMBIG_YAML)
    cases, placeholder_count = _strip_placeholders(raw_cases)
    if not cases:
        _write_ambig_results(
            [], Counter(),
            f"no real cases yet ({placeholder_count} placeholder(s); replace with real labels)"
        )
        pytest.skip(
            f"eval_capability_ambiguity.yaml has {placeholder_count} placeholder(s) "
            "and no real cases — replace placeholders with real human-labeled "
            "examples before this eval is meaningful."
        )

    from config import ConfigLoader
    from models.expense import Employee, EmployeeLevel, LineItem
    from agent.ambiguity_detector import AmbiguityDetector

    loader = ConfigLoader()
    detector = AmbiguityDetector(loader)
    results: list[dict] = []
    confusion = Counter()  # key: (gold, pred)

    for case in cases:
        case_id = case["id"]
        inp = case["input"]
        gold = case["human_gold"]

        d = inp["date"] if isinstance(inp["date"], date) else date.fromisoformat(str(inp["date"]))

        line_item = LineItem(
            expense_type=inp["expense_type"],
            amount=float(inp["amount"]),
            currency=inp.get("currency", "CNY"),
            city=inp.get("city", "上海"),
            date=d,
            invoice=None,
            description=inp.get("description", ""),
            attendees=inp.get("attendees", []),
        )
        employee = Employee(
            name="Human Eval Employee",
            id="human-eval-emp",
            department="Engineering",
            city=inp.get("city", "上海"),
            hire_date=date(2024, 1, 1),
            bank_account="6222000000000000",
            level=EmployeeLevel(inp.get("employee_level", "L3")),
        )
        history_items: list[LineItem] = []
        for h in inp.get("history", []):
            history_items.append(LineItem(
                expense_type=h.get("expense_type", inp["expense_type"]),
                amount=float(h["amount"]),
                currency=h.get("currency", "CNY"),
                city=h.get("city", inp.get("city", "上海")),
                date=date.fromisoformat(str(h["date"])),
                invoice=None,
                description=h.get("description", ""),
                attendees=h.get("attendees", []),
            ))

        out = detector.evaluate(
            line_item=line_item,
            employee=employee,
            rule_results=[],
            history=history_items,
        )
        pred = out.recommendation
        passed = pred == gold
        confusion[(gold, pred)] += 1

        results.append({
            "case_id": case_id,
            "description": case.get("description", ""),
            "input": inp,
            "ai_output": {
                "recommendation": pred,
                "score": out.score,
                "triggered_factors": list(out.triggered_factors),
                "explanation": out.explanation,
            },
            "human_gold": gold,
            "labeler_note": case.get("labeler_note", ""),
            "passed": passed,
        })

    _write_ambig_results(results, confusion, "deterministic")
    assert results


def _write_ambig_results(results: list[dict], confusion: Counter, mode: str) -> None:
    # Build 3x3 matrix with all cells populated (including zeros)
    matrix = {g: {p: confusion.get((g, p), 0) for p in _AMBIG_CLASSES} for g in _AMBIG_CLASSES}
    total = sum(confusion.values())
    correct = sum(confusion.get((c, c), 0) for c in _AMBIG_CLASSES)

    # Critical cells — the "重罪"
    false_negatives = confusion.get(("suggest_reject", "auto_pass"), 0)
    false_positives = confusion.get(("auto_pass", "suggest_reject"), 0)

    # Per-class precision / recall
    per_class: dict[str, dict] = {}
    for c in _AMBIG_CLASSES:
        tp = confusion.get((c, c), 0)
        fp = sum(confusion.get((g, c), 0) for g in _AMBIG_CLASSES if g != c)
        fn = sum(confusion.get((c, p), 0) for p in _AMBIG_CLASSES if p != c)
        precision = round(tp / (tp + fp), 3) if (tp + fp) else 0.0
        recall = round(tp / (tp + fn), 3) if (tp + fn) else 0.0
        per_class[c] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall}

    payload = {
        "kind": "ambiguity_human_eval",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "total_cases": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "critical_cells": {
            "gold_reject_pred_pass": false_negatives,
            "gold_pass_pred_reject": false_positives,
        },
        "confusion_matrix": matrix,
        "per_class": per_class,
        "results": results,
    }
    _write_json(_AMBIG_OUT, payload)
