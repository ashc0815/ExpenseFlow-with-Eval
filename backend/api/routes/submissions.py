"""提交报销单 API — 异步两步架构。

Step A: POST /api/submissions  → 202 + BackgroundTasks 启动管道
Step B: BackgroundTasks 跑 5-Skill 管道（agent / skills / rules）
Step C: GET  /api/submissions/{id} → 前端轮询
"""
from __future__ import annotations

import traceback
from datetime import date, datetime, timezone
from decimal import Decimal as _Decimal
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext, require_auth, require_role
from backend.api.routes.admin import _POLICY  # 为了拿 gl_mapping
from backend.db import store as _store
from backend.db.store import (
    create_audit_log, create_submission, get_employee, get_submission,
    get_submission_by_invoice, list_submissions, update_submission_analysis,
    update_submission_status, get_db,
)
from backend.storage import get_storage

router = APIRouter()


def _sub_dict(sub) -> dict[str, Any]:
    return {
        "id": sub.id,
        "employee_id": sub.employee_id,
        "status": sub.status,
        "amount": float(sub.amount),
        "currency": sub.currency,
        "category": sub.category,
        "date": sub.date,
        "merchant": sub.merchant,
        "tax_rate": float(sub.tax_rate) if sub.tax_rate else None,
        "tax_amount": float(sub.tax_amount) if sub.tax_amount else None,
        "project_code": sub.project_code,
        "description": sub.description,
        "receipt_url": sub.receipt_url,

        # ── 发票字段 ──
        "invoice_number": sub.invoice_number,
        "invoice_code": sub.invoice_code,
        "seller_tax_id": sub.seller_tax_id,
        "buyer_tax_id": sub.buyer_tax_id,

        # ── 派生字段 ──
        "department": sub.department,
        "cost_center": sub.cost_center,
        "gl_account": sub.gl_account,

        # ── AI 审核 ──
        "ocr_data": sub.ocr_data,
        "field_sources": (sub.ocr_data or {}).get("field_sources", {}) if sub.ocr_data else {},
        "audit_report": sub.audit_report,
        "risk_score": float(sub.risk_score) if sub.risk_score else None,
        "tier": sub.tier,

        # ── 经理审批 ──
        "approver_id": sub.approver_id,
        "approver_comment": sub.approver_comment,
        "approved_at": sub.approved_at.isoformat() if sub.approved_at else None,

        # ── 财务审批 ──
        "finance_approver_id": sub.finance_approver_id,
        "finance_approver_comment": sub.finance_approver_comment,
        "finance_approved_at": sub.finance_approved_at.isoformat() if sub.finance_approved_at else None,

        # ── 凭证 / 入账 ──
        "voucher_number": sub.voucher_number,
        "exported_at": sub.exported_at.isoformat() if sub.exported_at else None,
        "export_batch_id": sub.export_batch_id,

        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
    }


# 前端 category → agent/skills 标准 expense_type（subtype.id）
_CATEGORY_MAP = {
    "meal":          "meals",
    "transport":     "transport_local",
    "accommodation": "accommodation",
    "entertainment": "client_meal",
    "other":         "supplies",
}


async def _run_pipeline(submission_id: str, form_data: dict) -> None:
    """后台异步跑 5-Skill 管道，完成后回填数据库。"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from backend.config import DATABASE_URL

    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        from config import ConfigLoader
        from agent.controller import ExpenseController
        from models.expense import Employee, EmployeeLevel, ExpenseReport, LineItem, Invoice
        from models.enums import InvoiceType

        ConfigLoader.reset()
        loader = ConfigLoader()
        loader.load()

        level_str = form_data.get("level") or "L3"
        level_enum = getattr(EmployeeLevel, level_str, EmployeeLevel.L3)
        employee = Employee(
            id=form_data["employee_id"],
            name=form_data.get("employee_name") or form_data["employee_id"],
            department=form_data.get("department") or "综合部",
            city=(form_data.get("city") or "上海"),
            level=level_enum,
            hire_date=date(2020, 1, 1),
            bank_account="6222021234567890123",
        )
        expense_date = date.fromisoformat(form_data["date"])
        mapped_type = _CATEGORY_MAP.get(form_data["category"], "supplies")
        line_item = LineItem(
            description=form_data.get("description") or form_data["category"],
            expense_type=mapped_type,
            amount=float(form_data["amount"]),
            currency=form_data.get("currency", "CNY"),
            city=(form_data.get("city") or "上海"),
            date=expense_date,
            field_sources=dict(form_data.get("field_sources") or {}),
            invoice=Invoice(
                invoice_code=form_data.get("invoice_code") or "310012135012",
                invoice_number=form_data.get("invoice_number") or "12345678",
                invoice_type=InvoiceType.NORMAL,
                amount=float(form_data["amount"]),
                tax_amount=float(form_data.get("tax_amount") or 0),
                date=expense_date,
                vendor=form_data["merchant"],
                city=(form_data.get("city") or "上海"),
                buyer_name="示例科技有限公司",
            ),
        )
        report = ExpenseReport(
            report_id=submission_id,
            employee=employee,
            line_items=[line_item],
            total_amount=float(form_data["amount"]),
            submit_date=datetime.now(timezone.utc),
        )

        ctrl = ExpenseController(loader)
        result = ctrl.process_report(report)

        final_status = result.final_status.value
        tier_map = {
            "completed": "T1", "COMPLETED": "T1",
            "pending_review": "T3", "PENDING_REVIEW": "T3",
            "rejected": "T4", "REJECTED": "T4",
            "payment_failed": "T2", "PAYMENT_FAILED": "T2",
        }
        tier = tier_map.get(final_status, "T2")
        risk_score = {"T1": 20.0, "T2": 45.0, "T3": 70.0, "T4": 90.0}[tier]

        # ── Option B: 渐进式 timeline ──
        # 5-skill engine 仍然跑全 5 步（agent + skills 包），但提交阶段
        # 只持久化前 3 步（提交时能验证的事：发票、额度、合规）。凭证生成 / 付款
        # 执行将由 approve_submission / finance_approve 在实际审批通过后追加。
        # 这样 audit_report.timeline 永远只反映"已经发生的事"。
        SUBMIT_PHASE_LIMIT = 3
        # Pull rolled-up rule violations from the shield report (set by
        # ExpenseController._build_shield_report) up to the top level so
        # the AI explanation card and any future ERP push can read a flat
        # list of {rule_id, rule_text, severity, suggestion?} entries
        # without having to walk into per-line shield items.
        violations = []
        if result.shield_report and isinstance(result.shield_report, dict):
            violations = list(result.shield_report.get("violations") or [])

        # ── Agent compliance reasoner ──────────────────────────────────
        # Cross-record checks the deterministic pipeline can't do (leave
        # overlap, allowance conflict, cross-person meal). Failures are
        # appended to the same `violations` list with rule_id="agent.*"
        # so downstream UI / ERP push consumes one flat list.
        async with Session() as db:
            try:
                from agent.compliance_reasoner import reason_about_submission
                from agent.violation_registry import collect_agent_violations
                findings = await reason_about_submission(
                    db,
                    submission_id=submission_id,
                    employee_id=form_data["employee_id"],
                    expense_date=form_data["date"],
                    category=form_data["category"],
                    amount=float(form_data["amount"]),
                )
                agent_violations = collect_agent_violations(findings)
                if agent_violations:
                    # Bump risk if hard rules didn't already escalate. An
                    # agent.* finding is at least as severe as a yellow
                    # ambiguity flag — never silently downgrade T3/T4.
                    has_error = any(v.get("severity") == "error"
                                    for v in agent_violations)
                    if has_error and tier in ("T1", "T2"):
                        tier = "T3"
                        risk_score = max(risk_score, 70.0)
                violations.extend(agent_violations)
            except Exception:  # noqa: BLE001 — reasoner must never break pipeline
                pass

        # ── Layer 2 fraud investigator (OODA agent) ────────────────────
        # Triggered only on high-risk cases — combine ambiguity-driven
        # risk_score with the strongest Layer-1 fraud signal score, then
        # gate on >= 80 (option B from chat plan).
        # The OODA loop is in agent/fraud_investigator.py.
        investigation: Optional[dict] = None
        try:
            fraud_signals = []
            if result.shield_report and isinstance(result.shield_report, dict):
                # Layer 1 fraud rule signals — each {rule, score, evidence}
                fraud_signals = list(result.shield_report.get("fraud_signals") or [])
            fraud_max_score = max(
                (s.get("score", 0) for s in fraud_signals), default=0,
            )
            combined_risk = max(risk_score, float(fraud_max_score))
            if combined_risk >= 80:
                from agent.fraud_investigator import investigate_submission
                async with Session() as db:
                    investigation = await investigate_submission(
                        db,
                        submission={
                            "employee_id": form_data["employee_id"],
                            "date": form_data["date"],
                            "category": form_data["category"],
                            "amount": float(form_data["amount"]),
                            "currency": form_data.get("currency", "CNY"),
                            "merchant": form_data["merchant"],
                            "city": form_data.get("city", "上海"),
                            "description": form_data.get("description", ""),
                        },
                        fraud_signals=fraud_signals,
                        risk_score=combined_risk,
                    )
        except Exception:  # noqa: BLE001 — investigator must never break pipeline
            pass

        audit_report = {
            "final_status": final_status,
            "timeline": [
                {
                    "message": s.message,
                    "passed": s.passed,
                    "skipped": s.skipped,
                    "phase": "submit",
                }
                for s in result.timeline[:SUBMIT_PHASE_LIMIT]
            ],
            "shield_report": result.shield_report,
            "violations": violations,
        }
        if investigation is not None:
            audit_report["investigation"] = investigation

        async with Session() as db:
            await update_submission_analysis(
                db, submission_id,
                audit_report=audit_report,
                risk_score=risk_score,
                tier=tier,
                status="reviewed",
            )
            await create_audit_log(
                db, actor_id="system", action="ai_review_complete",
                resource_type="submission", resource_id=submission_id,
                detail={"tier": tier, "risk_score": risk_score},
            )

    except Exception as exc:
        tb = traceback.format_exc()
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from backend.config import DATABASE_URL as DB_URL
        engine2 = create_async_engine(DB_URL)
        Session2 = async_sessionmaker(engine2, expire_on_commit=False)
        async with Session2() as db:
            await update_submission_status(db, submission_id, "review_failed")
            await create_audit_log(
                db, actor_id="system", action="ai_review_failed",
                resource_type="submission", resource_id=submission_id,
                detail={"error": str(exc), "traceback": tb[:3000]},
            )
        await engine2.dispose()
    finally:
        await engine.dispose()


# ── POST /api/submissions ─────────────────────────────────────────

@router.post("", status_code=202)
async def submit_expense(
    background_tasks: BackgroundTasks,
    receipt_image: UploadFile = File(...),
    amount: float = Form(...),
    currency: str = Form("CNY"),
    category: str = Form(...),
    date: str = Form(...),
    merchant: str = Form(...),
    tax_rate: Optional[float] = Form(None),
    tax_amount: Optional[float] = Form(None),
    project_code: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    invoice_number: Optional[str] = Form(None),
    invoice_code: Optional[str] = Form(None),
    seller_tax_id: Optional[str] = Form(None),
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    # ── 1. 发票号去重（P5）──
    if invoice_number:
        existing = await get_submission_by_invoice(db, invoice_number)
        if existing:
            raise HTTPException(
                status_code=422,
                detail=f"发票号 {invoice_number} 已被报销过（单据 #{existing.id[:8]}）",
            )

    # ── 2. 保存发票文件 ──
    storage = get_storage()
    receipt_url = await storage.save(receipt_image, receipt_image.filename or "receipt.jpg")

    # ── 3. 派生字段 — 员工档案 + GL 映射 ──
    emp = await get_employee(db, ctx.user_id)
    department  = emp.department  if emp else None
    cost_center = emp.cost_center if emp else None
    gl_account  = (_POLICY.get("gl_mapping") or {}).get(category)

    # ── 预算检查 ────────────────────────────────────────────────────
    _budget_status: dict = {"configured": False}
    _budget_blocked = False
    try:
        if cost_center:
            _budget_status = await _store.get_budget_status(db, cost_center, _Decimal(str(amount)))
            _sig = _budget_status.get("signal")
            if _sig == "blocked":
                _budget_blocked = True
            elif _sig == "over_budget" and _budget_status.get("over_budget_action") == "block":
                _budget_blocked = True
    except Exception:
        _budget_blocked = False

    # ── 4. 落库 ──
    sub = await create_submission(db, {
        "employee_id": ctx.user_id,
        "status": "processing",
        "amount": amount,
        "currency": currency,
        "category": category,
        "date": date,
        "merchant": merchant,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "project_code": project_code,
        "description": description,
        "receipt_url": receipt_url,
        "invoice_number": invoice_number,
        "invoice_code": invoice_code,
        "seller_tax_id": seller_tax_id,
        "department": department,
        "cost_center": cost_center,
        "gl_account": gl_account,
        "budget_blocked": _budget_blocked,
    })
    await create_audit_log(
        db, actor_id=ctx.user_id, action="submission_created",
        resource_type="submission", resource_id=sub.id,
        detail={"amount": amount, "category": category, "merchant": merchant},
    )

    background_tasks.add_task(_run_pipeline, sub.id, {
        "employee_id": ctx.user_id,
        "employee_name": emp.name if emp else None,
        "department": department,
        "city": emp.city if emp else None,
        "level": emp.level if emp else None,
        "amount": amount, "currency": currency,
        "category": category, "date": date, "merchant": merchant,
        "tax_amount": tax_amount, "description": description,
        "invoice_number": invoice_number, "invoice_code": invoice_code,
    })

    return {
        "id": sub.id,
        "status": sub.status,
        "budget_blocked": sub.budget_blocked,
        "budget_status": _budget_status if _budget_status.get("configured") else None,
        "message": "报销单已提交，AI 正在审核中",
    }


# ── PATCH /api/submissions/{id}/unblock ──────────────────────────

@router.patch("/{submission_id}/unblock", status_code=200)
async def unblock_submission_endpoint(
    submission_id: str,
    ctx: UserContext = Depends(require_role("finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    sub = await _store.get_submission(db, submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if not sub.budget_blocked:
        raise HTTPException(status_code=400, detail="该报销单未被预算拦截")
    sub = await _store.unblock_submission(db, submission_id, ctx.user_id)
    return {"id": sub.id, "budget_blocked": sub.budget_blocked}


# ── GET /api/submissions/{id} ─────────────────────────────────────

@router.get("/{submission_id}")
async def get_submission_detail(
    submission_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    sub = await get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if ctx.role == "employee" and sub.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="无权查看该报销单")
    return _sub_dict(sub)


# ── GET /api/submissions ──────────────────────────────────────────

@router.get("")
async def list_expense_submissions(
    status: Optional[str] = None,
    exported: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    employee_id = ctx.user_id if ctx.role == "employee" else None
    result = await list_submissions(
        db, employee_id=employee_id, status=status,
        exported=exported, page=page, page_size=page_size,
    )
    result["items"] = [_sub_dict(s) for s in result["items"]]
    return result
