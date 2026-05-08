"""Status enums for Submission and Report.

These replace the 54 string literals scattered across the codebase per
``docs/code-health-audit.md`` Hotspot #4. ``StrEnum`` (3.11+) means
``SubmissionStatus.MANAGER_APPROVED == "manager_approved"`` is True, so
existing code comparing to string literals keeps working during the
migration. Once all call sites are converted, the literal comparisons
go away.

Why two enums (not one shared): Submission and Report have **different**
state machines. A submission can be ``in_report`` (sitting inside a
draft report) but a report has no such state. Sharing the enum would
permit illegal states (a Report claiming to be ``processing`` is
nonsense). Keep them separate.
"""
from __future__ import annotations

from enum import StrEnum


class SubmissionStatus(StrEnum):
    """Lifecycle of an individual expense line item.

    ``processing`` → ``reviewed`` / ``review_failed`` (5-Skill pipeline)
    ``reviewed`` → ``in_report`` (added to a draft report)
    ``in_report`` → ``manager_approved`` / ``rejected`` / ``needs_revision``
    ``manager_approved`` → ``finance_approved`` / ``rejected``
    ``finance_approved`` → ``exported``
    """

    PROCESSING = "processing"
    REVIEWED = "reviewed"
    REVIEW_FAILED = "review_failed"
    IN_REPORT = "in_report"
    MANAGER_APPROVED = "manager_approved"
    FINANCE_APPROVED = "finance_approved"
    EXPORTED = "exported"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class ReportStatus(StrEnum):
    """Lifecycle of a manager-reviewed report (a bundle of submissions).

    ``open`` → ``pending`` (employee submits)
    ``pending`` → ``manager_approved`` / ``rejected`` / ``needs_revision``
    ``manager_approved`` → ``finance_approved`` / ``rejected``
    ``finance_approved`` → ``exported``
    Plus ``withdrawn`` (employee recall before manager review).
    """

    OPEN = "open"
    PENDING = "pending"
    MANAGER_APPROVED = "manager_approved"
    FINANCE_APPROVED = "finance_approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"
    EXPORTED = "exported"
    WITHDRAWN = "withdrawn"


# Convenience: which submission states an approver may act on.
# Replaces the magic ``_MANAGER_ACTIONABLE`` tuple in approvals.py.
MANAGER_ACTIONABLE_SUBMISSION: frozenset[SubmissionStatus] = frozenset({
    SubmissionStatus.PROCESSING,
    SubmissionStatus.REVIEWED,
    SubmissionStatus.REVIEW_FAILED,
    SubmissionStatus.IN_REPORT,
})


__all__ = [
    "SubmissionStatus",
    "ReportStatus",
    "MANAGER_ACTIONABLE_SUBMISSION",
]
