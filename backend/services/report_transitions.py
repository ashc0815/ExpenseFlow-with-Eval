"""Report-level approval transitions.

Routes own HTTP concerns; this module owns the report/submission state changes,
audit trail writes, and side effects for approval workflow transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.store import (
    Report,
    append_audit_step,
    create_audit_log,
    create_notification,
    list_report_submissions,
    next_voucher_number,
)
from backend.domain.status import (
    MANAGER_ACTIONABLE_SUBMISSION_STATUSES,
    REPORT_FINANCE_APPROVED,
    REPORT_MANAGER_APPROVED,
    REPORT_NEEDS_REVISION,
    REPORT_PENDING,
    REPORT_REJECTED,
    SUBMISSION_FINANCE_APPROVED,
    SUBMISSION_MANAGER_APPROVED,
    SUBMISSION_NEEDS_REVISION,
    SUBMISSION_REJECTED,
)


@dataclass
class TransitionResult:
    report: Report
    line_count: int
    voucher_number: Optional[str] = None


class ReportTransitionError(Exception):
    """Raised when a report transition is not allowed."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _require_report_status(report: Report, expected: str, action_label: str) -> None:
    if report.status != expected:
        raise ReportTransitionError(f"当前状态 {report.status}，不可{action_label}")


async def manager_approve_report(
    db: AsyncSession,
    report: Report,
    *,
    actor_id: str,
    comment: Optional[str] = None,
) -> TransitionResult:
    _require_report_status(report, REPORT_PENDING, "审批")
    subs = await list_report_submissions(db, report.id)
    now = datetime.now(timezone.utc)
    for sub in subs:
        if sub.status in MANAGER_ACTIONABLE_SUBMISSION_STATUSES:
            sub.status = SUBMISSION_MANAGER_APPROVED
            sub.approver_id = actor_id
            sub.approver_comment = comment
            sub.approved_at = now
            sub.updated_at = now
            await append_audit_step(
                db,
                sub.id,
                message=f"经理 {actor_id} 整单批准",
                phase=SUBMISSION_MANAGER_APPROVED,
            )

    report.status = REPORT_MANAGER_APPROVED
    report.updated_at = now
    await db.commit()
    await create_audit_log(
        db,
        actor_id=actor_id,
        action="report_approved",
        resource_type="report",
        resource_id=report.id,
        detail={"comment": comment, "line_count": len(subs)},
    )
    return TransitionResult(report=report, line_count=len(subs))


async def manager_reject_report(
    db: AsyncSession,
    report: Report,
    *,
    actor_id: str,
    comment: Optional[str] = None,
) -> TransitionResult:
    _require_report_status(report, REPORT_PENDING, "拒绝")
    subs = await list_report_submissions(db, report.id)
    now = datetime.now(timezone.utc)
    for sub in subs:
        if sub.status in MANAGER_ACTIONABLE_SUBMISSION_STATUSES:
            sub.status = SUBMISSION_REJECTED
            sub.approver_id = actor_id
            sub.approver_comment = comment
            sub.updated_at = now

    report.status = REPORT_REJECTED
    report.updated_at = now
    await db.commit()
    await create_audit_log(
        db,
        actor_id=actor_id,
        action="report_rejected",
        resource_type="report",
        resource_id=report.id,
        detail={"comment": comment, "line_count": len(subs)},
    )
    return TransitionResult(report=report, line_count=len(subs))


async def return_report_for_revision(
    db: AsyncSession,
    report: Report,
    *,
    actor_id: str,
    reason: str,
) -> TransitionResult:
    if report.employee_id == actor_id:
        raise ReportTransitionError("不能退回自己的报销单")
    _require_report_status(report, REPORT_PENDING, "退回")
    subs = await list_report_submissions(db, report.id)
    now = datetime.now(timezone.utc)
    for sub in subs:
        if sub.status in MANAGER_ACTIONABLE_SUBMISSION_STATUSES:
            sub.status = SUBMISSION_NEEDS_REVISION
            sub.approver_id = actor_id
            sub.approver_comment = reason
            sub.updated_at = now

    report.status = REPORT_NEEDS_REVISION
    report.revision_reason = reason
    report.updated_at = now
    await db.commit()
    await create_notification(
        db,
        recipient_id=report.employee_id,
        kind="report_returned",
        title=f"报销单「{report.title}」被退回修改",
        body=f"退回原因：{reason}",
        link=f"/employee/report.html?report_id={report.id}",
    )
    await create_audit_log(
        db,
        actor_id=actor_id,
        action="report_returned",
        resource_type="report",
        resource_id=report.id,
        detail={"reason": reason, "line_count": len(subs)},
    )
    return TransitionResult(report=report, line_count=len(subs))


async def finance_approve_report(
    db: AsyncSession,
    report: Report,
    *,
    actor_id: str,
    comment: Optional[str] = None,
    bulk: bool = False,
) -> TransitionResult:
    _require_report_status(report, REPORT_MANAGER_APPROVED, "财务审批")
    subs = await list_report_submissions(db, report.id)
    now = datetime.now(timezone.utc)
    voucher = await next_voucher_number(db, report_id=report.id)
    for sub in subs:
        if sub.status == SUBMISSION_MANAGER_APPROVED:
            sub.status = SUBMISSION_FINANCE_APPROVED
            sub.finance_approver_id = actor_id
            sub.finance_approver_comment = comment
            sub.finance_approved_at = now
            sub.voucher_number = voucher
            sub.updated_at = now
            if not bulk:
                await append_audit_step(
                    db,
                    sub.id,
                    message=f"财务 {actor_id} 整单批准，凭证号 {voucher}",
                    phase=SUBMISSION_FINANCE_APPROVED,
                )

    report.status = REPORT_FINANCE_APPROVED
    report.voucher_number = voucher
    report.voucher_posted_at = now
    report.updated_at = now
    await db.commit()
    detail = {"comment": comment, "voucher": voucher}
    if bulk:
        detail["bulk"] = True
    else:
        detail["line_count"] = len(subs)
    await create_audit_log(
        db,
        actor_id=actor_id,
        action="report_finance_approved",
        resource_type="report",
        resource_id=report.id,
        detail=detail,
    )
    return TransitionResult(report=report, line_count=len(subs), voucher_number=voucher)


async def finance_reject_report(
    db: AsyncSession,
    report: Report,
    *,
    actor_id: str,
    comment: Optional[str] = None,
) -> TransitionResult:
    _require_report_status(report, REPORT_MANAGER_APPROVED, "拒绝")
    subs = await list_report_submissions(db, report.id)
    now = datetime.now(timezone.utc)
    for sub in subs:
        if sub.status == SUBMISSION_MANAGER_APPROVED:
            sub.status = SUBMISSION_REJECTED
            sub.finance_approver_id = actor_id
            sub.finance_approver_comment = comment
            sub.updated_at = now

    report.status = REPORT_REJECTED
    report.updated_at = now
    await db.commit()
    await create_audit_log(
        db,
        actor_id=actor_id,
        action="report_finance_rejected",
        resource_type="report",
        resource_id=report.id,
        detail={"comment": comment, "line_count": len(subs)},
    )
    return TransitionResult(report=report, line_count=len(subs))

