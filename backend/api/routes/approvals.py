"""审批 API — 经理审批 / 拒绝报销单。

路由挂载在 /api/submissions 前缀下：
  POST /{id}/approve         经理通过 → manager_approved
  POST /{id}/reject          经理拒绝 → rejected
  POST /bulk-approve         批量通过

经理只看 status in MANAGER_ACTIONABLE_SUBMISSION；通过后转给财务。

Refactor note (Gap 9 / Hotspot #1): the ``load → check status → mutate
→ audit`` pattern is now centralised:
  - status legality: ``backend.domain.transitions.ensure_submission_transition``
  - audit logging:   ``backend.services.audit.audit_event``
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext, require_role
from backend.api.routes.submissions import _sub_dict
from backend.db.store import get_db, get_submission, update_submission_status
from backend.domain.status import MANAGER_ACTIONABLE_SUBMISSION, SubmissionStatus
from backend.domain.transitions import (
    IllegalTransition, ensure_submission_transition,
)
from backend.services.audit import audit_event

router = APIRouter()


class ApproveBody(BaseModel):
    comment: Optional[str] = None


class RejectBody(BaseModel):
    comment: Optional[str] = None


class BulkApproveBody(BaseModel):
    ids: List[str]
    comment: Optional[str] = None


# ── POST /{id}/approve ────────────────────────────────────────────

@router.post("/{submission_id}/approve")
async def approve_submission(
    submission_id: str,
    body: ApproveBody = ApproveBody(),
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    sub = await get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_submission_transition(sub.status, SubmissionStatus.MANAGER_APPROVED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    updated = await update_submission_status(
        db, submission_id, SubmissionStatus.MANAGER_APPROVED.value,
        approver_id=ctx.user_id,
        approver_comment=body.comment,
    )
    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="manager_approved",
        resource_type="submission",
        resource_id=submission_id,
        detail={"comment": body.comment},
        timeline_message=f"凭证已生成（经理 {ctx.user_id} 批准）",
        timeline_phase=SubmissionStatus.MANAGER_APPROVED.value,
    )
    return _sub_dict(updated)


# ── POST /{id}/reject ─────────────────────────────────────────────

@router.post("/{submission_id}/reject")
async def reject_submission(
    submission_id: str,
    body: RejectBody = RejectBody(),
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    sub = await get_submission(db, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="报销单不存在")
    try:
        ensure_submission_transition(sub.status, SubmissionStatus.REJECTED)
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    updated = await update_submission_status(
        db, submission_id, SubmissionStatus.REJECTED.value,
        approver_id=ctx.user_id,
        approver_comment=body.comment,
    )
    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="manager_rejected",
        resource_type="submission",
        resource_id=submission_id,
        detail={"comment": body.comment},
    )
    return _sub_dict(updated)


# ── POST /bulk-approve ────────────────────────────────────────────

@router.post("/bulk-approve")
async def bulk_approve(
    body: BulkApproveBody,
    ctx: UserContext = Depends(require_role("manager", "finance_admin")),
    db: AsyncSession = Depends(get_db),
):
    results = {"approved": [], "skipped": [], "not_found": []}
    for sid in body.ids:
        sub = await get_submission(db, sid)
        if not sub:
            results["not_found"].append(sid)
            continue
        try:
            ensure_submission_transition(sub.status, SubmissionStatus.MANAGER_APPROVED)
        except IllegalTransition:
            results["skipped"].append({"id": sid, "status": sub.status})
            continue
        await update_submission_status(
            db, sid, SubmissionStatus.MANAGER_APPROVED.value,
            approver_id=ctx.user_id,
            approver_comment=body.comment,
        )
        await audit_event(
            db,
            actor_id=ctx.user_id,
            action="manager_approved",
            resource_type="submission",
            resource_id=sid,
            detail={"bulk": True, "comment": body.comment},
            timeline_message=f"凭证已生成（经理 {ctx.user_id} 批量批准）",
            timeline_phase=SubmissionStatus.MANAGER_APPROVED.value,
        )
        results["approved"].append(sid)
    return results
