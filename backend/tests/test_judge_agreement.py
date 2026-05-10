"""Judge agreement — Cohen's κ between AI components and human labels.

THE single most important Hamel-aligned eval gap this project has had:
without measuring agreement between the LLM (or deterministic) judge
and human ground truth, every accuracy number on the dashboard is
uncalibrated noise.

What this test does (per Hamel "Your AI Product Needs Evals"):

  1. Load human-labeled cases from eval_datasets/*_human_labeled.yaml
  2. Run the system under test (AmbiguityDetector / fraud_llm_analyzer)
  3. Compute confusion matrix vs human labels
  4. Compute Cohen's κ — accounts for agreement-by-chance, the right
     number for inter-rater agreement (raw accuracy is misleading
     when classes are imbalanced).
  5. Assert κ ≥ THRESHOLD_KAPPA. Fail when system disagrees with
     humans more than chance allows.

Cohen's κ scale (Landis & Koch 1977):
    < 0.20   poor — basically random
    0.20-0.40   fair — barely useful
    0.40-0.60   moderate — usable for triage
    0.60-0.80   substantial — production-ready
    0.80+    almost perfect — gold standard

Threshold policy:
  - Initial bar: 0.40 (moderate). Permissive while datasets are sparse.
  - Bump to 0.60 once each component has ≥30 real labeled cases (Hamel
    saturation guideline).
  - Each test asserts ≥ THRESHOLD_KAPPA; below that the AI's "pass rate"
    on the dashboard for that component is not trustworthy.

When this test fails:
  - DO NOT optimize the system to game the test.
  - First read the cases where AI ≠ human. Read the labeler_note for
    those cases. Are the human labels right? Is the AI prompt missing
    a constraint? Update either the system OR the test labels — but
    document which.

Output for the dashboard:
  Writes JSON snapshots that /api/eval/judge-agreement (TBD) can serve:
    backend/tests/eval_judge_ambiguity_latest.json
    backend/tests/eval_judge_fraud_overall_risk_latest.json
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest
import yaml


_TEST_DIR = Path(__file__).resolve().parent
_FRAUD_YAML = _TEST_DIR / "eval_datasets" / "eval_capability_fraud_llm.yaml"
_AMBIG_YAML = _TEST_DIR / "eval_datasets" / "eval_capability_ambiguity.yaml"
_AMBIG_OUT = _TEST_DIR / "eval_judge_ambiguity_latest.json"
_FRAUD_OUT = _TEST_DIR / "eval_judge_fraud_overall_risk_latest.json"


# ── Config ────────────────────────────────────────────────────────────

THRESHOLD_KAPPA = 0.40   # See module docstring for the scale.


# ── Env setup (only if not already configured by a parent run) ────────

os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", tempfile.gettempdir())
if "DATABASE_URL" not in os.environ:
    _tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _tmp_db.close()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp_db.name}"


# ── Helpers (local copies; intentionally independent of test_human_eval
#     so this module can run standalone and the two evals decouple) ───

def _load_yaml(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _is_placeholder(case: dict) -> bool:
    """A case is a placeholder when it carries no human judgment.
    Pattern: id contains `_placeholder_` OR description starts with
    'PLACEHOLDER'. Including placeholders would dilute κ toward chance."""
    return (
        "_placeholder_" in str(case.get("id", ""))
        or str(case.get("description", "")).startswith("PLACEHOLDER")
    )


def _strip_placeholders(cases: list[dict]) -> tuple[list[dict], int]:
    real = [c for c in cases if not _is_placeholder(c)]
    return real, len(cases) - len(real)


def _json_default(o: Any) -> str:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def _write_snapshot(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


# ── Cohen's κ + confusion matrix ──────────────────────────────────────

def cohens_kappa(rater_a: list, rater_b: list) -> float:
    """Cohen's κ for two equal-length rater label lists.

    κ = (po - pe) / (1 - pe)
        po = observed agreement
        pe = expected agreement by chance, given each rater's marginals.

    Edge cases:
      - empty lists → 0.0 (no signal)
      - perfect agreement when pe == 1 (single class) → 1.0
    """
    if not rater_a or not rater_b or len(rater_a) != len(rater_b):
        return 0.0
    n = len(rater_a)
    po = sum(1 for a, b in zip(rater_a, rater_b) if a == b) / n

    classes = sorted({*rater_a, *rater_b})
    pe = 0.0
    for c in classes:
        p_a = sum(1 for x in rater_a if x == c) / n
        p_b = sum(1 for x in rater_b if x == c) / n
        pe += p_a * p_b

    if pe >= 1.0:
        return 1.0  # all rated the same class; agreement undefined → trivially perfect
    return round((po - pe) / (1 - pe), 4)


def confusion_matrix(
    human: list, system: list, classes: Iterable[str],
) -> dict[str, dict[str, int]]:
    """Nested dict matrix[human_class][system_class] = count."""
    classes = list(classes)
    m: dict[str, dict[str, int]] = {h: {s: 0 for s in classes} for h in classes}
    for h, s in zip(human, system):
        if h in m and s in m[h]:
            m[h][s] += 1
    return m


def kappa_band(kappa: float) -> str:
    """Landis & Koch verbal label."""
    if kappa < 0.20:
        return "poor"
    if kappa < 0.40:
        return "fair"
    if kappa < 0.60:
        return "moderate"
    if kappa < 0.80:
        return "substantial"
    return "almost_perfect"


# ── Test 1: AmbiguityDetector recommendation agreement ───────────────

_AMBIG_CLASSES = ("auto_pass", "human_review", "suggest_reject")


def _run_ambiguity_detector(case_input: dict) -> str:
    """Return the AmbiguityDetector's recommendation for one case.
    The detector itself is the system under test; the question is
    whether its (auto_pass / human_review / suggest_reject) call
    matches what a human reviewer would say."""
    from config import ConfigLoader
    from models.expense import Employee, EmployeeLevel, LineItem
    from agent.ambiguity_detector import AmbiguityDetector

    loader = ConfigLoader()
    detector = AmbiguityDetector(loader)

    inp = case_input
    d = inp["date"] if isinstance(inp["date"], date) else date.fromisoformat(str(inp["date"]))
    line_item = LineItem(
        description=inp.get("description", ""),
        expense_type=inp.get("expense_type", "meals"),
        amount=float(inp.get("amount", 0)),
        currency=inp.get("currency", "CNY"),
        city=inp.get("city", "上海"),
        date=d,
    )
    employee = Employee(
        id="judge-agreement-emp",
        name="judge_agreement",
        department=inp.get("department", "综合部"),
        city=inp.get("city", "上海"),
        level=getattr(EmployeeLevel, inp.get("level", "L3"), EmployeeLevel.L3),
        hire_date=date(2020, 1, 1),
        bank_account="6222021234567890123",
    )

    result = detector.evaluate(line_item, employee, rule_results=[], history=[])
    return result.recommendation


def test_ambiguity_recommendation_kappa() -> None:
    """AmbiguityDetector vs human recommendation — must clear κ ≥ 0.40."""
    raw = _load_yaml(_AMBIG_YAML)
    cases, n_placeholder = _strip_placeholders(raw)
    if not cases:
        _write_snapshot(_AMBIG_OUT, {
            "empty": True,
            "placeholder_count": n_placeholder,
            "message": (
                f"{n_placeholder} placeholder(s); replace with real labels "
                "in eval_datasets/eval_capability_ambiguity.yaml"
            ),
        })
        pytest.skip(
            f"eval_capability_ambiguity.yaml has only {n_placeholder} "
            "placeholder(s). Add real human-labeled cases to compute κ."
        )

    human_labels: list[str] = []
    system_labels: list[str] = []
    per_case: list[dict] = []
    for case in cases:
        case_id = case["id"]
        inp = case["input"]
        gold = case["human_gold"]["recommendation"]
        try:
            actual = _run_ambiguity_detector(inp)
        except Exception as exc:  # noqa: BLE001 — record but continue
            per_case.append({"id": case_id, "error": str(exc), "human": gold})
            continue
        human_labels.append(gold)
        system_labels.append(actual)
        per_case.append({
            "id": case_id,
            "human": gold,
            "system": actual,
            "agree": gold == actual,
        })

    kappa = cohens_kappa(human_labels, system_labels)
    matrix = confusion_matrix(human_labels, system_labels, _AMBIG_CLASSES)
    band = kappa_band(kappa)
    sample_n = len(human_labels)

    _write_snapshot(_AMBIG_OUT, {
        "component": "ambiguity_detector",
        "metric": "cohens_kappa",
        "kappa": kappa,
        "band": band,
        "threshold": THRESHOLD_KAPPA,
        "sample_size": sample_n,
        "classes": list(_AMBIG_CLASSES),
        "confusion_matrix": matrix,
        "per_case": per_case,
        "skipped_placeholders": n_placeholder,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })

    assert sample_n >= 1, "no usable labeled cases survived after errors"
    assert kappa >= THRESHOLD_KAPPA, (
        f"AmbiguityDetector vs human recommendation κ={kappa} ({band}) "
        f"below threshold {THRESHOLD_KAPPA}. Read disagreeing cases in "
        f"{_AMBIG_OUT} (per_case[].agree==False) before tuning weights."
    )


# ── Test 2: fraud_llm_analyzer overall_risk agreement ────────────────

_FRAUD_RISK_CLASSES = ("clean", "suspicious", "fraud")


def _make_submission_row(inp: dict):
    from backend.services.fraud_rules import SubmissionRow
    return SubmissionRow(
        id=f"ja-{str(inp.get('description', 'x'))[:20]}",
        employee_id="judge-agreement-emp",
        description=inp.get("description", ""),
        category=inp.get("category", "meal"),
        amount=float(inp.get("amount", 0.0)),
        currency=inp.get("currency", "CNY"),
        merchant=inp.get("merchant", "unknown"),
        city=inp.get("city", "上海"),
        date=str(inp.get("date", date.today().isoformat())),
    )


def _derive_overall_risk(ai: dict) -> str:
    """Same heuristic as test_human_eval._derive_overall_risk — kept in
    sync intentionally so two evals on the same 6 subfields can disagree
    only on κ vs accuracy, not on the risk-aggregation rule."""
    def _bucket(score: Any) -> str:
        try:
            s = float(score)
        except (TypeError, ValueError):
            return "low"
        if s < 30:
            return "low"
        if s < 70:
            return "medium"
        return "high"

    template = _bucket(ai.get("template_score"))
    vagueness = _bucket(ai.get("vagueness_score"))
    reasonable = ai.get("person_amount_reasonable", True)
    contradiction = bool(ai.get("contradiction_found"))

    if reasonable is False:
        return "fraud"
    if template == "high" and vagueness == "high":
        return "fraud"
    if template == "high" or contradiction or vagueness == "high":
        return "suspicious"
    return "clean"


def _run_fraud_analyzer(inp: dict) -> str:
    """Run fraud LLM analyzer (or its mock) and reduce 6 subfields → overall_risk."""
    use_real = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("AGENT_USE_REAL_LLM") == "1"
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
        # Mock — neutral subfield scores. With real labeled data plus
        # AGENT_USE_REAL_LLM=1, this branch is bypassed; we keep it so
        # the test still runs (and snapshots) in CI without API keys.
        ai_output = {
            "template_score": 20.0,
            "contradiction_found": False,
            "extracted_person_count": None,
            "person_amount_reasonable": True,
            "vagueness_score": 20.0,
        }
    return _derive_overall_risk(ai_output if isinstance(ai_output, dict) else ai_output.__dict__)


def test_fraud_overall_risk_kappa() -> None:
    """fraud_llm_analyzer-derived overall_risk vs human label."""
    raw = _load_yaml(_FRAUD_YAML)
    cases, n_placeholder = _strip_placeholders(raw)
    if not cases:
        _write_snapshot(_FRAUD_OUT, {
            "empty": True,
            "placeholder_count": n_placeholder,
            "message": (
                f"{n_placeholder} placeholder(s); replace with real labels "
                "in eval_datasets/eval_capability_fraud_llm.yaml"
            ),
        })
        pytest.skip(
            f"eval_capability_fraud_llm.yaml has only {n_placeholder} "
            "placeholder(s). Add real human-labeled cases to compute κ."
        )

    human_labels: list[str] = []
    system_labels: list[str] = []
    per_case: list[dict] = []
    for case in cases:
        case_id = case["id"]
        inp = case["input"]
        human_overall = case["human_label"].get("overall_risk")
        if human_overall not in _FRAUD_RISK_CLASSES:
            per_case.append({"id": case_id, "error": f"unrecognized human label {human_overall!r}"})
            continue
        try:
            ai_overall = _run_fraud_analyzer(inp)
        except Exception as exc:  # noqa: BLE001
            per_case.append({"id": case_id, "error": str(exc), "human": human_overall})
            continue
        human_labels.append(human_overall)
        system_labels.append(ai_overall)
        per_case.append({
            "id": case_id,
            "human": human_overall,
            "system": ai_overall,
            "agree": human_overall == ai_overall,
        })

    kappa = cohens_kappa(human_labels, system_labels)
    matrix = confusion_matrix(human_labels, system_labels, _FRAUD_RISK_CLASSES)
    band = kappa_band(kappa)
    sample_n = len(human_labels)
    use_real = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("AGENT_USE_REAL_LLM") == "1"

    _write_snapshot(_FRAUD_OUT, {
        "component": "fraud_llm_analyzer",
        "metric": "cohens_kappa",
        "subfield": "overall_risk",
        "kappa": kappa,
        "band": band,
        "threshold": THRESHOLD_KAPPA,
        "sample_size": sample_n,
        "classes": list(_FRAUD_RISK_CLASSES),
        "confusion_matrix": matrix,
        "per_case": per_case,
        "skipped_placeholders": n_placeholder,
        "used_real_llm": use_real,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })

    assert sample_n >= 1, "no usable labeled cases survived after errors"
    if not use_real:
        # Mock LLM produces constant outputs → κ trivially 0 with diverse
        # human labels. Don't fail the threshold in mock mode; just record
        # the snapshot. With AGENT_USE_REAL_LLM=1 the assertion is binding.
        pytest.skip(
            "Mock LLM is constant-output; κ assertion requires real LLM "
            "(set AGENT_USE_REAL_LLM=1 + OPENAI_API_KEY). Snapshot written."
        )
    assert kappa >= THRESHOLD_KAPPA, (
        f"fraud_llm_analyzer vs human overall_risk κ={kappa} ({band}) "
        f"below threshold {THRESHOLD_KAPPA}. Read disagreeing cases in "
        f"{_FRAUD_OUT} (per_case[].agree==False) before tuning the prompt."
    )


# ── Pure-function tests (always run; verify the math) ────────────────


def test_cohens_kappa_perfect_agreement() -> None:
    assert cohens_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0


def test_cohens_kappa_zero_agreement() -> None:
    """κ ≤ 0 when raters do no better than chance."""
    a = ["x", "y", "x", "y"]
    b = ["y", "x", "y", "x"]   # systematically opposite
    assert cohens_kappa(a, b) <= 0.0


def test_cohens_kappa_single_class_is_one() -> None:
    """When everyone agrees on one class, κ is undefined → return 1."""
    assert cohens_kappa(["a", "a"], ["a", "a"]) == 1.0


def test_cohens_kappa_band_boundaries() -> None:
    assert kappa_band(0.05) == "poor"
    assert kappa_band(0.30) == "fair"
    assert kappa_band(0.50) == "moderate"
    assert kappa_band(0.70) == "substantial"
    assert kappa_band(0.85) == "almost_perfect"


def test_confusion_matrix_basic() -> None:
    h = ["clean", "fraud", "clean"]
    s = ["clean", "clean", "clean"]
    m = confusion_matrix(h, s, ["clean", "suspicious", "fraud"])
    assert m["clean"]["clean"] == 2
    assert m["fraud"]["clean"] == 1
    assert m["fraud"]["fraud"] == 0
