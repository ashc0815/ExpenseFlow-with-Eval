"""Agent compliance reasoner — eval against the human-labeled dataset.

Loads `eval_datasets/eval_regression_agent_compliance.yaml`, seeds each
case's `context` (leaves / allowances / cross-person attendee records)
into a fresh DB, runs `reason_about_submission()` on the case's
submission, and compares findings to `human_label.expected_findings`.

Aligned with the same Hamel principle as `test_judge_agreement.py`:
without a human-labeled ground truth set, "the reasoner works" is a
vibe, not a number.

Per-case use of unique `employee_id` and (where applicable) unique
`other_submission.id` keeps cases isolated — no fixture teardown
between cases.

Skip behavior matches the project-wide convention (Stage 1):
  - All cases placeholder → SKIP with a clear "replace placeholders"
    message; no fake-pass numbers.
  - At least one real case → fail-on-disagreement.
"""
from __future__ import annotations

import asyncio
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

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from agent.compliance_reasoner import reason_about_submission
from backend.db.store import (
    Base, EvalBase, add_employee_leave, add_submission_attendee,
    create_submission, upsert_employee_allowance,
)


_DATASET_PATH = (
    Path(__file__).resolve().parent
    / "eval_datasets"
    / "eval_regression_agent_compliance.yaml"
)

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
    """Placeholder marker: id contains `_placeholder_` or labeler_note ==
    'PLACEHOLDER'. Same convention as the other human_labeled YAMLs."""
    return (
        "_placeholder_" in str(case.get("id", ""))
        or str(case.get("labeler_note", "")).strip().upper() == "PLACEHOLDER"
    )


def _strip_placeholders(cases: list[dict]) -> tuple[list[dict], int]:
    real = [c for c in cases if not _is_placeholder(c)]
    return real, len(cases) - len(real)


def _to_date(d) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


async def _seed_context(db, case_id: str, employee_id: str, ctx: dict) -> None:
    """Seed the context the reasoner will query.

    `ctx` shape (all keys optional):
      leaves: [{start_date, end_date, kind, status}]
      allowances: [{kind, monthly_amount, effective_from, effective_to}]
      other_submission: {id, employee_id, date, category, amount, merchant}
      attendees: [{submission_id, name, role, employee_id?}]
                  (when employee_id is omitted, use the case's submitter
                  so the cross-person rule has a hit; otherwise pass
                  through verbatim)
    """
    for leave in ctx.get("leaves", []) or []:
        await add_employee_leave(
            db, employee_id=employee_id,
            start_date=_to_date(leave["start_date"]),
            end_date=_to_date(leave["end_date"]),
            kind=leave.get("kind", "vacation"),
            status=leave.get("status", "approved"),
        )

    for a in ctx.get("allowances", []) or []:
        await upsert_employee_allowance(
            db, employee_id=employee_id,
            kind=a["kind"],
            monthly_amount=Decimal(str(a["monthly_amount"])),
            effective_from=_to_date(a["effective_from"]),
            effective_to=_to_date(a["effective_to"]) if a.get("effective_to") else None,
        )

    other_sub = ctx.get("other_submission")
    if other_sub:
        await create_submission(db, {
            "id": other_sub["id"],
            "employee_id": other_sub["employee_id"],
            "status": other_sub.get("status", "finance_approved"),
            "amount": Decimal(str(other_sub["amount"])),
            "currency": other_sub.get("currency", "CNY"),
            "category": other_sub["category"],
            "date": str(other_sub["date"]),
            "merchant": other_sub["merchant"],
            "receipt_url": other_sub.get("receipt_url", "/uploads/test/x.jpg"),
        })

    for att in ctx.get("attendees", []) or []:
        # When employee_id is omitted on the attendee, wire the case's own
        # employee (the common pattern: "self appears on someone else's list")
        attendee_emp_id = att.get("employee_id", employee_id)
        await add_submission_attendee(
            db,
            submission_id=att["submission_id"],
            name=att.get("name", "Test User"),
            employee_id=attendee_emp_id,
            role=att.get("role"),
        )


def _assert_findings_match(actual: list[dict], expected: list[dict], case_id: str):
    """Compare actual reasoner findings vs expected list. Order-insensitive
    on (kind), tolerant of extra fields in actual findings (we only check
    kind + severity)."""
    actual_kinds = sorted([f["kind"] for f in actual])
    expected_kinds = sorted([f["kind"] for f in expected])
    assert actual_kinds == expected_kinds, (
        f"[{case_id}] reasoner findings mismatch.\n"
        f"  expected kinds: {expected_kinds}\n"
        f"  actual kinds:   {actual_kinds}\n"
        f"  full actual:    {actual}"
    )

    # Severity check (only when expected explicitly says one)
    expected_by_kind = {f["kind"]: f for f in expected if "severity" in f}
    actual_by_kind = {f["kind"]: f for f in actual}
    for kind, exp in expected_by_kind.items():
        actual_finding = actual_by_kind.get(kind)
        # Severity in the reasoner output lives in the violation registry,
        # not in the raw finding dict — so we check it via collect_agent_violations
        from agent.violation_registry import collect_agent_violations
        violations = collect_agent_violations([actual_finding])
        if violations:
            assert violations[0].get("severity") == exp["severity"], (
                f"[{case_id}] severity mismatch for {kind}: "
                f"expected {exp['severity']}, got {violations[0].get('severity')}"
            )


# ── The eval test ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_compliance_against_human_labels():
    raw = _load_cases()
    cases, n_placeholder = _strip_placeholders(raw)
    if not cases:
        pytest.skip(
            f"eval_regression_agent_compliance.yaml has only {n_placeholder} "
            "placeholder(s) and no real cases yet. Replace with real labels."
        )

    failures: list[str] = []
    passed = 0
    skipped = 0

    for case in cases:
        case_id = case["id"]
        ctx = case.get("context") or {}
        sub = case.get("submission") or {}
        expected = (case.get("human_label") or {}).get("expected_findings", [])

        # Skip cases with empty submission (defensive — schema requires it
        # but we don't want one bad case to torch the whole run)
        if not sub.get("category") or not sub.get("date"):
            skipped += 1
            continue

        # Per-case unique employee id keeps cases fully isolated
        employee_id = f"emp_eval_{case_id}"

        async with _Session() as db:
            await _seed_context(db, case_id, employee_id, ctx)

            # The reasoner doesn't need the actual submission row to exist
            # in the DB — it only takes the values as args. We pass
            # submission_id=None because the case-under-test has no row.
            try:
                findings = await reason_about_submission(
                    db,
                    submission_id=None,
                    employee_id=employee_id,
                    expense_date=str(sub["date"]),
                    category=sub["category"],
                    amount=float(sub.get("amount", 0)),
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"[{case_id}] reasoner threw: {exc}")
                continue

        try:
            _assert_findings_match(findings, expected, case_id)
            passed += 1
        except AssertionError as exc:
            failures.append(str(exc))

    if failures:
        pytest.fail(
            f"{len(failures)}/{len(cases)} agent compliance cases disagreed "
            f"with human labels (skipped {n_placeholder} placeholders, "
            f"{skipped} malformed):\n\n"
            + "\n\n".join(failures)
        )

    # If we got here, every real case agreed with the human label.
    # Report counts to stderr so the dashboard can pick up the run later.
    import sys
    sys.stderr.write(
        f"\n  ✓ agent_compliance_eval: {passed}/{len(cases)} real cases agreed "
        f"({n_placeholder} placeholder(s) skipped)\n"
    )
