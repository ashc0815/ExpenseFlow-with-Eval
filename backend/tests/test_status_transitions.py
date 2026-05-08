"""Unit tests for the domain state machine.

Validates that the legal-transition tables in
``backend.domain.transitions`` reject illegal moves and accept legal
ones. These run with no DB / no FastAPI / no async — pure functions.

Why this matters: before this PR, status legality was enforced inline
in 9 endpoints with slightly different ``allowed_set`` tuples. A typo
in any one of them would silently allow an illegal transition. Now
there's one source of truth, and these tests are how we keep it
correct.
"""
from __future__ import annotations

import pytest

from backend.domain.status import ReportStatus, SubmissionStatus
from backend.domain.transitions import (
    IllegalTransition,
    ensure_report_transition,
    ensure_submission_transition,
)


# ─────────────────────────────────────────────────────────────────────
# Submission transitions — happy path
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,target", [
    (SubmissionStatus.PROCESSING, SubmissionStatus.REVIEWED),
    (SubmissionStatus.PROCESSING, SubmissionStatus.REVIEW_FAILED),
    (SubmissionStatus.REVIEWED, SubmissionStatus.IN_REPORT),
    (SubmissionStatus.REVIEWED, SubmissionStatus.MANAGER_APPROVED),
    (SubmissionStatus.REVIEWED, SubmissionStatus.REJECTED),
    (SubmissionStatus.IN_REPORT, SubmissionStatus.MANAGER_APPROVED),
    (SubmissionStatus.IN_REPORT, SubmissionStatus.REJECTED),
    (SubmissionStatus.IN_REPORT, SubmissionStatus.NEEDS_REVISION),
    (SubmissionStatus.MANAGER_APPROVED, SubmissionStatus.FINANCE_APPROVED),
    (SubmissionStatus.MANAGER_APPROVED, SubmissionStatus.REJECTED),
    (SubmissionStatus.FINANCE_APPROVED, SubmissionStatus.EXPORTED),
    (SubmissionStatus.NEEDS_REVISION, SubmissionStatus.IN_REPORT),
])
def test_submission_legal_transitions(current, target):
    assert ensure_submission_transition(current, target) == target


# ─────────────────────────────────────────────────────────────────────
# Submission transitions — illegal moves
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,target,reason", [
    # Can't skip manager approval
    (SubmissionStatus.REVIEWED, SubmissionStatus.FINANCE_APPROVED,
     "finance must come after manager"),
    # Can't un-export
    (SubmissionStatus.EXPORTED, SubmissionStatus.MANAGER_APPROVED,
     "exported is terminal"),
    # Can't reverse a rejection
    (SubmissionStatus.REJECTED, SubmissionStatus.MANAGER_APPROVED,
     "rejected is terminal"),
    # Can't go from finance back to manager
    (SubmissionStatus.FINANCE_APPROVED, SubmissionStatus.MANAGER_APPROVED,
     "finance is later than manager"),
    # Can't approve from processing without review (review path is required)
    # Note: PROCESSING → MANAGER_APPROVED is allowed as a fallback for
    # human override, but PROCESSING → FINANCE_APPROVED is not.
    (SubmissionStatus.PROCESSING, SubmissionStatus.FINANCE_APPROVED,
     "must go through manager first"),
])
def test_submission_illegal_transitions(current, target, reason):
    with pytest.raises(IllegalTransition):
        ensure_submission_transition(current, target)


def test_submission_unknown_status_raises():
    with pytest.raises(IllegalTransition):
        ensure_submission_transition("wibble", SubmissionStatus.MANAGER_APPROVED)


def test_submission_accepts_string_input():
    """Endpoints pass DB strings, not enum instances. Verify both work."""
    result = ensure_submission_transition("reviewed", "manager_approved")
    assert result == SubmissionStatus.MANAGER_APPROVED


# ─────────────────────────────────────────────────────────────────────
# Report transitions — happy path
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,target", [
    (ReportStatus.OPEN, ReportStatus.PENDING),
    (ReportStatus.PENDING, ReportStatus.MANAGER_APPROVED),
    (ReportStatus.PENDING, ReportStatus.REJECTED),
    (ReportStatus.PENDING, ReportStatus.NEEDS_REVISION),
    (ReportStatus.MANAGER_APPROVED, ReportStatus.FINANCE_APPROVED),
    (ReportStatus.MANAGER_APPROVED, ReportStatus.REJECTED),
    (ReportStatus.FINANCE_APPROVED, ReportStatus.EXPORTED),
    (ReportStatus.NEEDS_REVISION, ReportStatus.OPEN),
    (ReportStatus.NEEDS_REVISION, ReportStatus.PENDING),
    (ReportStatus.WITHDRAWN, ReportStatus.OPEN),
])
def test_report_legal_transitions(current, target):
    assert ensure_report_transition(current, target) == target


# ─────────────────────────────────────────────────────────────────────
# Report transitions — illegal moves
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current,target,reason", [
    # Can't approve a draft
    (ReportStatus.OPEN, ReportStatus.MANAGER_APPROVED,
     "must submit (pending) first"),
    # Can't skip manager approval
    (ReportStatus.PENDING, ReportStatus.FINANCE_APPROVED,
     "finance comes after manager"),
    # Can't un-export
    (ReportStatus.EXPORTED, ReportStatus.MANAGER_APPROVED,
     "exported is terminal"),
    # Can't reverse a rejection
    (ReportStatus.REJECTED, ReportStatus.MANAGER_APPROVED,
     "rejected is terminal"),
])
def test_report_illegal_transitions(current, target, reason):
    with pytest.raises(IllegalTransition):
        ensure_report_transition(current, target)
