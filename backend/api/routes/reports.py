"""Reports routes — employees bundle multiple line items into a single report,
then submit / withdraw the whole package."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from backend.api.middleware.auth import UserContext, require_auth, require_role
from backend.api.routes.submissions import _sub_dict
from backend.db.store import (
    Employee, Report, Submission,
    append_audit_step, create_audit_log, create_notification, create_report,
    get_db, get_employee, get_or_create_open_report, get_report,
    list_report_drafts, list_report_submissions, list_reports_for_employee,
    next_voucher_number, set_report_status, update_submission_finance,
    update_submission_status,
)
from backend.domain.status import ReportStatus, SubmissionStatus
from backend.domain.transitions import (
    IllegalTransition, ensure_report_transition, ensure_submission_transition,
)
from backend.quick.finalize import finalize_report
from backend.services.audit import audit_event

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────

class NewReportBody(BaseModel):
    title: Optional[str] = None


class ApproveReportBody(BaseModel):
    comment: Optional[str] = None


class ReturnReportBody(BaseModel):
    reason: str


class BulkApproveBody(BaseModel):
    ids: list[str]
    comment: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────

def _report_dict(r) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "status": r.status,
        "revision_reason": r.revision_reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
        "withdrawn_at": r.withdrawn_at.isoformat() if r.withdrawn_at else None,
    }


def _line_dict(s: Submission) -> dict:
    return {
        "id": s.id,
        "status": s.status,
        "merchant": s.merchant,
        "amount": float(s.amount),
        "currency": s.currency,
        "exchange_rate": float(s.exchange_rate) if s.exchange_rate is not None else None,
        "converted_amount": float(s.converted_amount) if s.converted_amount is not None else None,
        "category": s.category,
        "date": s.date,
        "receipt_url": s.receipt_url,
        "invoice_number": s.invoice_number,
        "invoice_code": s.invoice_code,
        "seller_tax_id": s.seller_tax_id,
        "buyer_tax_id": s.buyer_tax_id,
        "tax_amount": float(s.tax_amount) if s.tax_amount is not None else None,
        "project_code": s.project_code,
        "description": s.description,
        "department": s.department,
        "cost_center": s.cost_center,
        "gl_account": s.gl_account,
        "risk_score": float(s.risk_score) if s.risk_score is not None else None,
        "tier": s.tier,
        "audit_report": s.audit_report,
        "approver_comment": s.approver_comment,
        "finance_approver_comment": s.finance_approver_comment,
    }


def _draft_line_dict(d) -> dict:
    fields = d.fields or {}
    return {
        "id": d.id,
        "status": "draft",
        "merchant": fields.get("merchant"),
        "amount": float(fields["amount"]) if fields.get("amount") is not None else None,
        "currency": fields.get("currency", "CNY"),
        "category": fields.get("category"),
        "date": fields.get("date"),
        "receipt_url": d.receipt_url,
        "layer": d.layer,
    }


async def _report_payload(db: AsyncSession, report) -> dict:
    from backend.services.fx_service import get_rate, convert as fx_convert
    subs = await list_report_submissions(db, report.id)
    drafts = await list_report_drafts(db, report.id)
    total = sum(float(s.amount) for s in subs)

    emp = await get_employee(db, report.employee_id)
    home_currency = emp.home_currency if emp and emp.home_currency else "CNY"

    lines = []
    total_converted = 0.0
    for s in subs:
        line = _line_dict(s)
        currency = s.currency or "CNY"
        amt = float(s.amount)
        if s.exchange_rate is not None:
            rate = float(s.exchange_rate)
            conv = round(amt * rate, 2)
        else:
            rate = get_rate(currency, home_currency)
            conv = fx_convert(amt, currency, home_currency)
        line["exchange_rate"] = rate
        line["converted_amount"] = conv
        total_converted += conv
        lines.append(line)

    draft_lines = []
    for d in drafts:
        dl = _draft_line_dict(d)
        currency = dl.get("currency") or "CNY"
        amt = dl.get("amount")
        if amt is not None:
            rate = get_rate(currency, home_currency)
            conv = fx_convert(float(amt), currency, home_currency)
            dl["exchange_rate"] = rate
            dl["converted_amount"] = conv
            total_converted += conv
        else:
            dl["exchange_rate"] = None
            dl["converted_amount"] = None
        draft_lines.append(dl)

    return {
        **_report_dict(report),
        "lines": lines,
        "pending_drafts": draft_lines,
        "total_amount": total,
        "total_converted": round(total_converted, 2),
        "home_currency": home_currency,
        "line_count": len(subs),
    }


async def _manager_id_for(db: AsyncSession, employee_id: str) -> Optional[str]:
    emp = await get_employee(db, employee_id)
    return emp.manager_id if emp and emp.manager_id else None


# ── Routes ───────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_new_report(
    body: NewReportBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if ctx.role != "employee":
        raise HTTPException(status_code=403, detail="仅员工可创建报销单")
    report = await create_report(db, ctx.user_id, title=body.title or "新建报销单")
    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_created",
        resource_type="report", resource_id=report.id,
        detail={"title": report.title},
    )
    return await _report_payload(db, report)


@router.get("")
async def list_my_reports(
    status: Optional[str] = None,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if ctx.role != "employee":
        raise HTTPException(status_code=403, detail="仅员工可查看")
    reports = await list_reports_for_employee(db, ctx.user_id, status=status)
    items = []
    for r in reports:
        items.append(await _report_payload(db, r))
    return {"items": items, "total": len(items)}


@router.get("/open")
async def get_my_open_report(
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    if ctx.role != "employee":
        raise HTTPException(status_code=403, detail="仅员工可查看")
    report = await get_or_create_open_report(db, ctx.user_id)
    return await _report_payload(db, report)


@router.get("/queue/pending")
async def manager_report_queue(
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    """All reports in 'pending' state — the manager's work queue."""
    result = await db.execute(
        select(Report)
        .where(Report.status == "pending")
        .order_by(Report.submitted_at.asc())
    )
    reports = list(result.scalars().all())
    from backend.services.fx_service import get_rate, convert as fx_convert

    items = []
    for r in reports:
        subs = await list_report_submissions(db, r.id)
        emp = await get_employee(db, r.employee_id)
        home_currency = emp.home_currency if emp and emp.home_currency else "CNY"

        total = sum(float(s.amount) for s in subs)
        total_converted = 0.0
        lines = []
        for s in subs:
            line = _line_dict(s)
            currency = s.currency or "CNY"
            amt = float(s.amount)
            if s.exchange_rate is not None:
                rate = float(s.exchange_rate)
                conv = round(amt * rate, 2)
            else:
                rate = get_rate(currency, home_currency)
                conv = fx_convert(amt, currency, home_currency)
            line["exchange_rate"] = rate
            line["converted_amount"] = conv
            total_converted += conv
            lines.append(line)

        max_risk = max((float(s.risk_score) for s in subs if s.risk_score is not None), default=0)
        worst_tier = None
        tier_order = {"T4": 4, "T3": 3, "T2": 2, "T1": 1}
        for s in subs:
            if s.tier and tier_order.get(s.tier, 0) > tier_order.get(worst_tier, 0):
                worst_tier = s.tier

        all_reviewed = all(
            s.status in ("reviewed", "review_failed") for s in subs
        )
        still_processing = any(s.status == "processing" for s in subs)

        items.append({
            **_report_dict(r),
            "employee_id": r.employee_id,
            "employee_name": emp.name if emp else r.employee_id,
            "department": emp.department if emp else None,
            "total_amount": total,
            "total_converted": round(total_converted, 2),
            "home_currency": home_currency,
            "line_count": len(subs),
            "lines": lines,
            "max_risk_score": max_risk,
            "worst_tier": worst_tier,
            "all_reviewed": all_reviewed,
            "still_processing": still_processing,
        })
    return {"items": items, "total": len(items)}


@router.get("/queue/finance")
async def finance_report_queue(
    ctx: UserContext = Depends(require_role("finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reports in 'manager_approved' state — the finance review queue."""
    from backend.services.fx_service import get_rate, convert as fx_convert

    result = await db.execute(
        select(Report)
        .where(Report.status == "manager_approved")
        .order_by(Report.updated_at.asc())
    )
    reports = list(result.scalars().all())
    items = []
    for r in reports:
        subs = await list_report_submissions(db, r.id)
        emp = await get_employee(db, r.employee_id)
        home_currency = emp.home_currency if emp and emp.home_currency else "CNY"

        total = sum(float(s.amount) for s in subs)
        total_converted = 0.0
        lines = []
        for s in subs:
            line = _line_dict(s)
            currency = s.currency or "CNY"
            amt = float(s.amount)
            if s.exchange_rate is not None:
                rate = float(s.exchange_rate)
                conv = round(amt * rate, 2)
            else:
                rate = get_rate(currency, home_currency)
                conv = fx_convert(amt, currency, home_currency)
            line["exchange_rate"] = rate
            line["converted_amount"] = conv
            total_converted += conv
            lines.append(line)

        max_risk = max((float(s.risk_score) for s in subs if s.risk_score is not None), default=0)
        worst_tier = None
        tier_order = {"T4": 4, "T3": 3, "T2": 2, "T1": 1}
        for s in subs:
            if s.tier and tier_order.get(s.tier, 0) > tier_order.get(worst_tier, 0):
                worst_tier = s.tier

        items.append({
            **_report_dict(r),
            "employee_id": r.employee_id,
            "employee_name": emp.name if emp else r.employee_id,
            "department": emp.department if emp else None,
            "total_amount": total,
            "total_converted": round(total_converted, 2),
            "home_currency": home_currency,
            "line_count": len(subs),
            "lines": lines,
            "max_risk_score": max_risk,
            "worst_tier": worst_tier,
        })
    return {"items": items, "total": len(items)}


@router.post("/{report_id}/finance-approve")
async def finance_approve_report(
    report_id: str,
    body: ApproveReportBody = ApproveReportBody(),
    ctx: UserContext = Depends(require_role("finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_report_transition(report.status, ReportStatus.FINANCE_APPROVED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    # Idempotent: passes report_id so a retry after partial failure reuses
    # the voucher number already attached to this report.
    voucher = await next_voucher_number(db, report_id=report_id)
    for s in subs:
        if s.status == SubmissionStatus.MANAGER_APPROVED.value:
            await update_submission_finance(
                db, s.id,
                status=SubmissionStatus.FINANCE_APPROVED.value,
                finance_approver_id=ctx.user_id,
                finance_approver_comment=body.comment,
                voucher_number=voucher,
            )
            await append_audit_step(
                db, s.id,
                message=f"财务 {ctx.user_id} 整单批准，凭证号 {voucher}",
                phase=SubmissionStatus.FINANCE_APPROVED.value,
            )

    # Per-report voucher: write the voucher number on the Report itself so
    # downstream readers (export page, audit, ERP push) treat the report as
    # the unit of voucher (one business event = one voucher).
    report.status = ReportStatus.FINANCE_APPROVED.value
    report.voucher_number = voucher
    report.voucher_posted_at = now
    report.updated_at = now
    await db.commit()

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="report_finance_approved",
        resource_type="report",
        resource_id=report_id,
        detail={"comment": body.comment, "voucher": voucher, "line_count": len(subs)},
    )
    return {"status": "ok", "voucher_number": voucher, "report_id": report_id}


@router.post("/{report_id}/finance-reject")
async def finance_reject_report(
    report_id: str,
    body: ApproveReportBody = ApproveReportBody(),
    ctx: UserContext = Depends(require_role("finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_report_transition(report.status, ReportStatus.REJECTED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    for s in subs:
        if s.status == SubmissionStatus.MANAGER_APPROVED.value:
            await update_submission_finance(
                db, s.id,
                status=SubmissionStatus.REJECTED.value,
                finance_approver_id=ctx.user_id,
                finance_approver_comment=body.comment,
            )

    report.status = ReportStatus.REJECTED.value
    report.updated_at = now
    await db.commit()

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="report_finance_rejected",
        resource_type="report",
        resource_id=report_id,
        detail={"comment": body.comment, "line_count": len(subs)},
    )
    return {"status": "ok", "report_id": report_id}


@router.post("/queue/finance/bulk-approve")
async def finance_bulk_approve_reports(
    body: BulkApproveBody,
    ctx: UserContext = Depends(require_role("finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Bulk approve low-risk reports at the report level."""
    results = {"approved": [], "skipped": []}
    for rid in body.ids:
        report = await get_report(db, rid)
        if not report:
            results["skipped"].append(rid)
            continue
        try:
            ensure_report_transition(report.status, ReportStatus.FINANCE_APPROVED)
        except IllegalTransition:
            results["skipped"].append(rid)
            continue
        subs = await list_report_submissions(db, rid)
        now = datetime.now(timezone.utc)
        voucher = await next_voucher_number(db, report_id=rid)
        for s in subs:
            if s.status == SubmissionStatus.MANAGER_APPROVED.value:
                await update_submission_finance(
                    db, s.id,
                    status=SubmissionStatus.FINANCE_APPROVED.value,
                    finance_approver_id=ctx.user_id,
                    finance_approver_comment=body.comment,
                    voucher_number=voucher,
                )
        report.status = ReportStatus.FINANCE_APPROVED.value
        report.voucher_number = voucher
        report.voucher_posted_at = now
        report.updated_at = now
        await db.commit()
        await audit_event(
            db,
            actor_id=ctx.user_id,
            action="report_finance_approved",
            resource_type="report",
            resource_id=rid,
            detail={"bulk": True, "comment": body.comment, "voucher": voucher},
        )
        results["approved"].append({"id": rid, "voucher": voucher})
    return results


@router.get("/{report_id}")
async def get_report_detail(
    report_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if ctx.role == "employee" and report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    return await _report_payload(db, report)


@router.post("/{report_id}/submit")
async def submit_report(
    report_id: str,
    background_tasks: BackgroundTasks,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    return await finalize_report(report_id, ctx, db, background_tasks)


@router.post("/{report_id}/withdraw")
async def withdraw_report(
    report_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if ctx.role != "employee" or report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    if report.status not in ("pending", "approved", "manager_approved",
                              "finance_approved", "rejected"):
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {report.status}，无法撤回",
        )

    was_approved = report.status in (
        "approved", "manager_approved", "finance_approved"
    )

    # Revert every line to in_report and clear approval state
    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    for s in subs:
        s.status = "in_report"
        s.approver_id = None
        s.approver_comment = None
        s.approved_at = None
        s.finance_approver_id = None
        s.finance_approver_comment = None
        s.finance_approved_at = None
        s.voucher_number = None
        s.exported_at = None
        s.export_batch_id = None
        s.audit_report = None
        s.risk_score = None
        s.tier = None
        s.updated_at = now
    report.status = "open"
    report.withdrawn_at = now
    report.submitted_at = None
    report.updated_at = now
    await db.commit()

    # Notify the employee's direct manager (if any)
    manager_id = await _manager_id_for(db, ctx.user_id)
    if manager_id:
        emp = await get_employee(db, ctx.user_id)
        title = f"{emp.name if emp else ctx.user_id} 撤回了报销单"
        body_text = (
            f"报销单「{report.title}」已被员工撤回。"
            f"原状态: {'已批准' if was_approved else '待审批'}，共 {len(subs)} 笔。"
        )
        await create_notification(
            db,
            recipient_id=manager_id,
            kind="report_withdrawn",
            title=title,
            body=body_text,
            link=f"/manager/queue.html?report={report_id}",
        )

    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_withdrawn",
        resource_type="report", resource_id=report_id,
        detail={
            "was_approved": was_approved,
            "line_count": len(subs),
            "notified_manager": manager_id,
        },
    )

    return {
        "report_id": report_id,
        "status": "open",
        "was_approved": was_approved,
        "reverted_lines": len(subs),
    }


# ── Manager endpoints ────────────────────────────────────────────

@router.post("/{report_id}/approve")
async def approve_report(
    report_id: str,
    body: ApproveReportBody = ApproveReportBody(),
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_report_transition(report.status, ReportStatus.MANAGER_APPROVED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    for s in subs:
        # Skip submissions that aren't in a manager-actionable state
        # (e.g. already approved, rejected). Domain-level filter via
        # ``MANAGER_ACTIONABLE_SUBMISSION`` keeps this list authoritative.
        if SubmissionStatus(s.status) not in {
            SubmissionStatus.PROCESSING,
            SubmissionStatus.REVIEWED,
            SubmissionStatus.REVIEW_FAILED,
        }:
            continue
        s.status = SubmissionStatus.MANAGER_APPROVED.value
        s.approver_id = ctx.user_id
        s.approver_comment = body.comment
        s.approved_at = now
        s.updated_at = now
        await append_audit_step(
            db, s.id,
            message=f"经理 {ctx.user_id} 整单批准",
            phase=SubmissionStatus.MANAGER_APPROVED.value,
        )

    report.status = ReportStatus.MANAGER_APPROVED.value
    report.updated_at = now
    await db.commit()

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="report_approved",
        resource_type="report",
        resource_id=report_id,
        detail={"comment": body.comment, "line_count": len(subs)},
    )
    return await _report_payload(db, report)


@router.post("/{report_id}/reject")
async def reject_report(
    report_id: str,
    body: ApproveReportBody = ApproveReportBody(),
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_report_transition(report.status, ReportStatus.REJECTED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    for s in subs:
        if SubmissionStatus(s.status) not in {
            SubmissionStatus.PROCESSING,
            SubmissionStatus.REVIEWED,
            SubmissionStatus.REVIEW_FAILED,
        }:
            continue
        s.status = SubmissionStatus.REJECTED.value
        s.approver_id = ctx.user_id
        s.approver_comment = body.comment
        s.updated_at = now

    report.status = ReportStatus.REJECTED.value
    report.updated_at = now
    await db.commit()

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="report_rejected",
        resource_type="report",
        resource_id=report_id,
        detail={"comment": body.comment, "line_count": len(subs)},
    )
    return await _report_payload(db, report)


@router.post("/{report_id}/return")
async def return_report_for_revision(
    report_id: str,
    body: ReturnReportBody,
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if report.employee_id == ctx.user_id:
        raise HTTPException(status_code=403, detail="不能退回自己的报销单")
    try:
        ensure_report_transition(report.status, ReportStatus.NEEDS_REVISION)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    subs = await list_report_submissions(db, report_id)
    now = datetime.now(timezone.utc)
    for s in subs:
        if SubmissionStatus(s.status) not in {
            SubmissionStatus.PROCESSING,
            SubmissionStatus.REVIEWED,
            SubmissionStatus.REVIEW_FAILED,
        }:
            continue
        s.status = SubmissionStatus.NEEDS_REVISION.value
        s.approver_id = ctx.user_id
        s.approver_comment = body.reason
        s.updated_at = now

    report.status = ReportStatus.NEEDS_REVISION.value
    report.revision_reason = body.reason
    report.updated_at = now
    await db.commit()

    await create_notification(
        db,
        recipient_id=report.employee_id,
        kind="report_returned",
        title=f"报销单「{report.title}」被退回修改",
        body=f"退回原因：{body.reason}",
        link=f"/employee/report.html?report_id={report_id}",
    )

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="report_returned",
        resource_type="report",
        resource_id=report_id,
        detail={"reason": body.reason, "line_count": len(subs)},
    )
    return await _report_payload(db, report)


@router.post("/{report_id}/recall")
async def recall_rejected_report(
    report_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """员工把被拒绝的报销单召回，状态变为 needs_revision 以便编辑重提。

    仅对 `rejected` 状态开放。不丢拒绝历史：
      - 审计日志写入 `report_recalled`（含原拒绝原因）
      - 原 approver/finance_approver 字段保留在 submission 上
      - 将拒绝原因写入 revision_reason，前端可复用现有退回横幅
    """
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="只能召回自己的报销单")
    if report.status != "rejected":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {report.status}，不可召回（仅支持 rejected）",
        )

    subs = await list_report_submissions(db, report_id)
    # 优先 finance 拒绝评论，回退到 manager 拒绝评论
    previous_reason = next(
        (s.finance_approver_comment or s.approver_comment for s in subs
         if (s.finance_approver_comment or s.approver_comment)),
        None,
    )

    now = datetime.now(timezone.utc)
    for s in subs:
        if s.status == "rejected":
            s.status = "needs_revision"
            s.updated_at = now

    report.status = "needs_revision"
    report.revision_reason = previous_reason or "员工已召回，准备修改重提"
    report.updated_at = now
    await db.commit()

    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_recalled",
        resource_type="report", resource_id=report_id,
        detail={"previous_reason": previous_reason, "line_count": len(subs)},
    )
    return await _report_payload(db, report)


@router.post("/{report_id}/resubmit")
async def resubmit_report(
    report_id: str,
    background_tasks: BackgroundTasks,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    if report.status != "needs_revision":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {report.status}，不可重新提交",
        )

    subs = await list_report_submissions(db, report_id)
    lines = [s for s in subs if s.status == "needs_revision"]
    if not lines:
        raise HTTPException(status_code=422, detail="没有需要重新提交的发票")

    emp = await get_employee(db, ctx.user_id)

    now = datetime.now(timezone.utc)
    for s in lines:
        s.status = "processing"
        s.approver_id = None
        s.approver_comment = None
        s.approved_at = None
        s.audit_report = None
        s.risk_score = None
        s.tier = None
        s.updated_at = now

    report.status = "pending"
    report.revision_reason = None
    report.submitted_at = now
    report.updated_at = now
    await db.commit()

    from backend.api.routes.submissions import _run_pipeline
    for s in lines:
        background_tasks.add_task(_run_pipeline, s.id, {
            "employee_id":    ctx.user_id,
            "employee_name":  emp.name if emp else None,
            "department":     s.department,
            "city":           emp.city if emp else None,
            "level":          emp.level if emp else None,
            "amount":         float(s.amount),
            "currency":       s.currency,
            "category":       s.category,
            "date":           s.date,
            "merchant":       s.merchant,
            "tax_amount":     float(s.tax_amount) if s.tax_amount is not None else None,
            "description":    s.description,
            "invoice_number": s.invoice_number,
            "invoice_code":   s.invoice_code,
        })

    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_resubmitted",
        resource_type="report", resource_id=report_id,
        detail={"line_count": len(lines)},
    )
    return {"report_id": report_id, "status": "pending", "lines": len(lines)}


# ── Line-item edit (finance) ─────────────────────────────────────

EDITABLE_FIELDS = {
    "merchant", "amount", "currency", "category", "date",
    "tax_amount", "project_code", "description",
    "invoice_number", "invoice_code", "seller_tax_id", "buyer_tax_id",
    "department", "cost_center", "gl_account",
    "exchange_rate",
}


class RenameBody(BaseModel):
    title: str


@router.patch("/{report_id}/title")
async def rename_report(
    report_id: str,
    body: RenameBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if report.employee_id != ctx.user_id:
        raise HTTPException(403, "Not your report")
    if report.status not in ("open", "needs_revision"):
        raise HTTPException(400, "Cannot rename a submitted report")
    report.title = body.title.strip() or report.title
    await db.commit()
    return {"ok": True, "title": report.title}


class PatchLineBody(BaseModel):
    field: str
    value: Any


@router.patch("/{report_id}/lines/{submission_id}")
async def patch_report_line(
    report_id: str,
    submission_id: str,
    body: PatchLineBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Edit individual fields on a submission line.

    Employees can edit lines in their own open reports.
    Manager/finance can edit lines in any pending+ report.
    """
    from backend.db.store import get_submission
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")

    if ctx.role == "employee":
        if report.employee_id != ctx.user_id:
            raise HTTPException(status_code=403, detail="权限不足")
        # `pending` is a locked state — employee must withdraw the report back
        # to `open` (or wait for manager to reject into `needs_revision`)
        # before any line edits are allowed.
        if report.status not in ("open", "needs_revision"):
            raise HTTPException(status_code=409, detail="报销单已提交或已审批，无法编辑（请先撤回）")
    elif ctx.role not in ("manager", "finance_admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    sub = await get_submission(db, submission_id)
    if not sub or sub.report_id != report_id:
        raise HTTPException(status_code=404, detail="行项目不存在")
    if body.field not in EDITABLE_FIELDS:
        raise HTTPException(status_code=422, detail=f"字段 {body.field} 不可编辑")

    old_value = getattr(sub, body.field, None)
    setattr(sub, body.field, body.value)
    if body.field == "exchange_rate" and body.value is not None:
        sub.converted_amount = round(float(sub.amount) * float(body.value), 2)
    elif body.field == "amount" and sub.exchange_rate is not None:
        sub.converted_amount = round(float(body.value) * float(sub.exchange_rate), 2)
    sub.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(sub)

    await create_audit_log(
        db, actor_id=ctx.user_id, action="line_field_edited",
        resource_type="submission", resource_id=submission_id,
        detail={
            "field": body.field,
            "old": str(old_value),
            "new": str(body.value),
            "report_id": report_id,
        },
    )
    return _line_dict(sub)


@router.delete("/{report_id}/lines/{submission_id}", status_code=200)
async def delete_report_line(
    report_id: str,
    submission_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Employee can delete a line item from their own open or needs_revision report.

    `pending` (submitted, awaiting manager approval) is locked — the employee
    must withdraw the report back to `open` before deletions are allowed.
    """
    from backend.db.store import get_submission
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if ctx.role == "employee":
        if report.employee_id != ctx.user_id:
            raise HTTPException(status_code=403, detail="权限不足")
        if report.status not in ("open", "needs_revision"):
            raise HTTPException(status_code=409, detail="报销单已提交或已审批，无法删除（请先撤回）")
    elif ctx.role not in ("manager", "finance_admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    sub = await get_submission(db, submission_id)
    if sub and sub.report_id == report_id:
        await db.delete(sub)
        await db.commit()
        await create_audit_log(
            db, actor_id=ctx.user_id, action="line_deleted",
            resource_type="submission", resource_id=submission_id,
            detail={"report_id": report_id, "merchant": sub.merchant, "amount": str(sub.amount)},
        )
        return {"ok": True, "deleted_id": submission_id}

    from backend.db.store import get_draft
    draft = await get_draft(db, submission_id)
    if draft and draft.report_id == report_id:
        await db.delete(draft)
        await db.commit()
        await create_audit_log(
            db, actor_id=ctx.user_id, action="draft_deleted",
            resource_type="draft", resource_id=submission_id,
            detail={"report_id": report_id},
        )
        return {"ok": True, "deleted_id": submission_id}

    raise HTTPException(status_code=404, detail="行项目不存在")


@router.delete("/{report_id}", status_code=200)
async def delete_report(
    report_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Delete an empty open report.

    Only the owning employee may delete, and only when the report is in
    `open` status with zero line items and zero drafts. Submitted, approved,
    or withdrawn reports retain audit history and cannot be deleted.
    """
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    if report.status != "open":
        raise HTTPException(status_code=409, detail="仅开放中的报销单可删除")

    subs = await list_report_submissions(db, report_id)
    drafts = await list_report_drafts(db, report_id)
    if subs or drafts:
        raise HTTPException(status_code=409, detail="请先清空报销单中的所有条目")

    title = report.title
    await db.delete(report)
    await db.commit()
    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_deleted",
        resource_type="report", resource_id=report_id,
        detail={"title": title},
    )
    return {"ok": True, "deleted_id": report_id}
