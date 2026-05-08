"""State machine for Submission + Report.

The legal-transition tables below are the **single source of truth** for
"can this status change?" Previously this knowledge was scattered across
9 endpoints' ``if sub.status not in (...)`` checks (see
``docs/code-health-audit.md`` Hotspot #1).

Usage at the call site:

    from backend.domain.status import SubmissionStatus
    from backend.domain.transitions import (
        IllegalTransition, ensure_submission_transition,
    )

    try:
        ensure_submission_transition(sub.status, SubmissionStatus.MANAGER_APPROVED)
    except IllegalTransition as exc:
        raise HTTPException(409, str(exc))

The function does **not** mutate or persist — it only validates. The
caller still owns the DB write. Keeping persistence out of the domain
layer means we can unit-test transitions with zero DB setup.
"""
from __future__ import annotations

from typing import Mapping

from backend.domain.status import ReportStatus, SubmissionStatus


class IllegalTransition(Exception):
    """Raised when a requested status change is not allowed by the state
    machine. Endpoints should map this to HTTP 409 Conflict."""


# ─────────────────────────────────────────────────────────────────────
# Submission state machine
# ─────────────────────────────────────────────────────────────────────

# For each current state, the set of legal next states.
# Empty set = terminal state.
_SUBMISSION_LEGAL: Mapping[SubmissionStatus, frozenset[SubmissionStatus]] = {
    SubmissionStatus.PROCESSING: frozenset({
        SubmissionStatus.REVIEWED,
        SubmissionStatus.REVIEW_FAILED,
        SubmissionStatus.MANAGER_APPROVED,  # 经理直接拍板（兜底）
        SubmissionStatus.REJECTED,
    }),
    SubmissionStatus.REVIEWED: frozenset({
        SubmissionStatus.IN_REPORT,
        SubmissionStatus.MANAGER_APPROVED,
        SubmissionStatus.REJECTED,
    }),
    SubmissionStatus.REVIEW_FAILED: frozenset({
        SubmissionStatus.MANAGER_APPROVED,  # 经理人工兜底
        SubmissionStatus.REJECTED,
        SubmissionStatus.IN_REPORT,
    }),
    SubmissionStatus.IN_REPORT: frozenset({
        SubmissionStatus.MANAGER_APPROVED,
        SubmissionStatus.REJECTED,
        SubmissionStatus.NEEDS_REVISION,
    }),
    SubmissionStatus.MANAGER_APPROVED: frozenset({
        SubmissionStatus.FINANCE_APPROVED,
        SubmissionStatus.REJECTED,  # 财务可推翻经理决策
    }),
    SubmissionStatus.FINANCE_APPROVED: frozenset({
        SubmissionStatus.EXPORTED,
    }),
    SubmissionStatus.NEEDS_REVISION: frozenset({
        SubmissionStatus.IN_REPORT,
        SubmissionStatus.PROCESSING,
    }),
    # Terminal states — no further transitions
    SubmissionStatus.REJECTED: frozenset(),
    SubmissionStatus.EXPORTED: frozenset(),
}


def ensure_submission_transition(
    current: str | SubmissionStatus,
    target: str | SubmissionStatus,
) -> SubmissionStatus:
    """Validate that ``current → target`` is legal for a Submission.

    Returns the resolved ``SubmissionStatus`` enum on success; raises
    ``IllegalTransition`` otherwise. Accepts plain strings for current
    (DB returns strings); the target is normalized to enum.
    """
    try:
        cur = SubmissionStatus(current) if not isinstance(current, SubmissionStatus) else current
    except ValueError as exc:
        raise IllegalTransition(f"未知的报销单状态 '{current}'") from exc
    try:
        nxt = SubmissionStatus(target) if not isinstance(target, SubmissionStatus) else target
    except ValueError as exc:
        raise IllegalTransition(f"未知的目标状态 '{target}'") from exc
    legal = _SUBMISSION_LEGAL.get(cur, frozenset())
    if nxt not in legal:
        raise IllegalTransition(
            f"报销单当前状态 '{cur.value}' 不能转为 '{nxt.value}'"
        )
    return nxt


# ─────────────────────────────────────────────────────────────────────
# Report state machine
# ─────────────────────────────────────────────────────────────────────

_REPORT_LEGAL: Mapping[ReportStatus, frozenset[ReportStatus]] = {
    ReportStatus.OPEN: frozenset({
        ReportStatus.PENDING,
        ReportStatus.WITHDRAWN,
    }),
    ReportStatus.PENDING: frozenset({
        ReportStatus.MANAGER_APPROVED,
        ReportStatus.REJECTED,
        ReportStatus.NEEDS_REVISION,
        ReportStatus.WITHDRAWN,  # 撤回（罕见但合法）
    }),
    ReportStatus.MANAGER_APPROVED: frozenset({
        ReportStatus.FINANCE_APPROVED,
        ReportStatus.REJECTED,
        ReportStatus.NEEDS_REVISION,
    }),
    ReportStatus.FINANCE_APPROVED: frozenset({
        ReportStatus.EXPORTED,
    }),
    ReportStatus.NEEDS_REVISION: frozenset({
        ReportStatus.OPEN,      # 员工重新打开编辑
        ReportStatus.PENDING,   # 直接重新提交
    }),
    # Terminal states
    ReportStatus.REJECTED: frozenset(),
    ReportStatus.EXPORTED: frozenset(),
    ReportStatus.WITHDRAWN: frozenset({
        ReportStatus.OPEN,  # 撤回后可重新编辑
    }),
}


def ensure_report_transition(
    current: str | ReportStatus,
    target: str | ReportStatus,
) -> ReportStatus:
    """Validate that ``current → target`` is legal for a Report.

    Returns the resolved ``ReportStatus`` enum on success; raises
    ``IllegalTransition`` otherwise.
    """
    try:
        cur = ReportStatus(current) if not isinstance(current, ReportStatus) else current
    except ValueError as exc:
        raise IllegalTransition(f"未知的报表状态 '{current}'") from exc
    try:
        nxt = ReportStatus(target) if not isinstance(target, ReportStatus) else target
    except ValueError as exc:
        raise IllegalTransition(f"未知的目标状态 '{target}'") from exc
    legal = _REPORT_LEGAL.get(cur, frozenset())
    if nxt not in legal:
        raise IllegalTransition(
            f"报表当前状态 '{cur.value}' 不能转为 '{nxt.value}'"
        )
    return nxt


__all__ = [
    "IllegalTransition",
    "ensure_submission_transition",
    "ensure_report_transition",
]
