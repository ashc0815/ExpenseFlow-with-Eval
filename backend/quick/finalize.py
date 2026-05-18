"""Helpers: save a draft as a report line, then finalize a whole report."""
from __future__ import annotations

from typing import Optional

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext
from backend.api.routes.admin import _POLICY
from backend.api.routes.submissions import _run_pipeline
from backend.db.store import (
    Submission, create_audit_log, create_submission, get_draft, get_employee,
    get_or_create_open_report, get_report, list_report_submissions,
    mark_draft_submitted,
)


async def save_draft_as_report_line(
    draft_id: str,
    ctx: UserContext,
    db: AsyncSession,
    *,
    report_id: Optional[str] = None,
) -> tuple[str, str]:
    """Attest: create a submission with status='in_report' and attach to a report.

    Returns (submission_id, report_id). Does NOT kick off the pipeline —
    that happens later when the whole report is submitted.
    """
    draft = await get_draft(db, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft 不存在")
    if draft.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    if draft.submitted_as:
        raise HTTPException(status_code=409, detail=f"该 draft 已保存为 {draft.submitted_as}")
    if not draft.receipt_url:
        raise HTTPException(status_code=422, detail="请先上传发票")

    fields = draft.fields or {}
    for required in ("merchant", "amount", "date", "category"):
        if not fields.get(required):
            raise HTTPException(status_code=422, detail=f"缺少必填字段：{required}")

    # Pick / create target report
    if report_id:
        report = await get_report(db, report_id)
        if not report or report.employee_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="报销单不存在")
        if report.status != "open":
            raise HTTPException(status_code=409, detail=f"报销单状态为 {report.status}，不可添加")
    else:
        report = await get_or_create_open_report(db, ctx.user_id)

    # Invoice dedup: reject if invoice already exists anywhere OTHER than in_report.
    inv = fields.get("invoice_number")
    if inv:
        result = await db.execute(
            select(Submission).where(Submission.invoice_number == inv)
        )
        conflicts = [s for s in result.scalars().all() if s.status != "in_report"]
        if conflicts:
            existing = conflicts[0]
            raise HTTPException(
                status_code=422,
                detail=f"发票号 {inv} 已被报销过（单据 #{existing.id[:8]}）",
            )

    emp = await get_employee(db, ctx.user_id)
    department  = emp.department  if emp else None
    cost_center = emp.cost_center if emp else None
    gl_account  = (_POLICY.get("gl_mapping") or {}).get(fields.get("category"))

    from backend.services.fx_service import get_rate

    emp_home = emp.home_currency if emp and hasattr(emp, 'home_currency') and emp.home_currency else "CNY"
    invoice_currency = fields.get("currency", "CNY")
    user_rate = fields.get("exchange_rate")

    if user_rate is not None:
        fx_rate = float(user_rate)
    elif invoice_currency != emp_home:
        fx_rate = get_rate(invoice_currency, emp_home)
    else:
        fx_rate = 1.0

    amount_val = float(fields["amount"])
    converted = round(amount_val * fx_rate, 2)

    sub = await create_submission(db, {
        "employee_id":    ctx.user_id,
        "status":         "in_report",
        "amount":         amount_val,
        "currency":       invoice_currency,
        "category":       fields["category"],
        "date":           fields["date"],
        "merchant":       fields["merchant"],
        "tax_amount":     float(fields.get("tax_amount") or 0) or None,
        "project_code":   fields.get("project_code"),
        "description":    fields.get("description"),
        "receipt_url":    draft.receipt_url,
        "ocr_data":       {"field_sources": dict(draft.field_sources or {})},
        "invoice_number": inv,
        "invoice_code":   fields.get("invoice_code"),
        "department":     department,
        "cost_center":    cost_center,
        "gl_account":     gl_account,
        "report_id":      report.id,
        "exchange_rate":  fx_rate,
        "converted_amount": converted,
    })
    await mark_draft_submitted(db, draft_id, sub.id)
    await create_audit_log(
        db, actor_id=ctx.user_id, action="draft_saved_to_report",
        resource_type="submission", resource_id=sub.id,
        detail={"draft_id": draft_id, "report_id": report.id},
    )
    return sub.id, report.id


async def finalize_report(
    report_id: str,
    ctx: UserContext,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
) -> dict:
    """Submit the whole report: flip every in_report line to 'processing' and
    kick off the review pipeline for each."""
    report = await get_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if report.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")
    if report.status != "open":
        raise HTTPException(status_code=409, detail=f"当前状态 {report.status}，不可提交")

    subs = await list_report_submissions(db, report_id)
    lines = [s for s in subs if s.status == "in_report"]
    if not lines:
        raise HTTPException(status_code=422, detail="报销单为空，请先添加发票")

    emp = await get_employee(db, ctx.user_id)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for s in lines:
        s.status = "processing"
        s.updated_at = now
    report.status = "pending"
    report.submitted_at = now
    report.updated_at = now
    await db.commit()

    for s in lines:
        field_sources = {}
        if isinstance(s.ocr_data, dict):
            field_sources = dict(s.ocr_data.get("field_sources") or {})
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
            "field_sources":  field_sources,
        })

    await create_audit_log(
        db, actor_id=ctx.user_id, action="report_submitted",
        resource_type="report", resource_id=report_id,
        detail={"line_count": len(lines)},
    )
    return {"report_id": report_id, "status": "pending", "lines": len(lines)}
