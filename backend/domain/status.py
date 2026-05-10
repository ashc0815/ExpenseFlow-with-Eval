"""Canonical report and submission status values."""

from __future__ import annotations

from typing import Final

SUBMISSION_PROCESSING: Final = "processing"
SUBMISSION_REVIEWED: Final = "reviewed"
SUBMISSION_REVIEW_FAILED: Final = "review_failed"
SUBMISSION_MANAGER_APPROVED: Final = "manager_approved"
SUBMISSION_FINANCE_APPROVED: Final = "finance_approved"
SUBMISSION_REJECTED: Final = "rejected"
SUBMISSION_NEEDS_REVISION: Final = "needs_revision"
SUBMISSION_IN_REPORT: Final = "in_report"

REPORT_OPEN: Final = "open"
REPORT_PENDING: Final = "pending"
REPORT_MANAGER_APPROVED: Final = "manager_approved"
REPORT_FINANCE_APPROVED: Final = "finance_approved"
REPORT_REJECTED: Final = "rejected"
REPORT_NEEDS_REVISION: Final = "needs_revision"

MANAGER_ACTIONABLE_SUBMISSION_STATUSES: Final = (
    SUBMISSION_PROCESSING,
    SUBMISSION_REVIEWED,
    SUBMISSION_REVIEW_FAILED,
)

