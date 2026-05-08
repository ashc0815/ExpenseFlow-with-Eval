"""Audit-event helper — single entry point for "log this state change".

Replaces the 15+ ad-hoc patterns documented in
``docs/code-health-audit.md`` Hotspot #5, where every endpoint hand-built
the same f-string + dict combo + double-call to ``append_audit_step`` +
``create_audit_log``.

Usage:

    from backend.services.audit import audit_event

    await audit_event(
        db,
        actor_id=ctx.user_id,
        action="manager_approved",
        resource_type="submission",
        resource_id=sub.id,
        detail={"comment": comment},
        timeline_message=f"凭证已生成（经理 {ctx.user_id} 批准）",
        timeline_phase="manager_approved",
    )

If ``timeline_message`` is None, only the ``audit_logs`` row is written.
This is the right behaviour for ``Report``-level events (Submission has
the embedded ``audit_report.timeline``; Report doesn't).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.store import append_audit_step, create_audit_log


async def audit_event(
    db: AsyncSession,
    *,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    detail: Optional[dict] = None,
    timeline_message: Optional[str] = None,
    timeline_phase: Optional[str] = None,
) -> None:
    """Write one audit_log row + (optionally) one timeline step.

    The two writes are not atomic — a crash between them leaves the
    audit_log row with no matching timeline entry. That's acceptable
    because the audit_log table is the source of truth; timeline is a
    UX convenience embedded on the Submission for the AI 解释卡.

    Two-step splits (only timeline, only log) were considered and
    rejected — every real state change wants both, splitting them just
    invites callers to forget one.
    """
    if timeline_message and resource_type == "submission":
        # Timeline lives on Submission.audit_report only. Reports don't
        # have a timeline; the audit_log table is their record.
        if timeline_phase is None:
            raise ValueError(
                "timeline_message requires timeline_phase (which approval "
                "stage this entry belongs to)"
            )
        await append_audit_step(
            db, resource_id,
            message=timeline_message,
            phase=timeline_phase,
        )
    await create_audit_log(
        db,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail or {},
    )


__all__ = ["audit_event"]
