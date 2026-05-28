"""Agent Chat API — 员工和 AI 助手对话式报销。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext, require_auth
from backend.agents.expense_assistant.llm import BaseLLM, LLMResponse, MockLLM, RealLLM, get_llm
from backend.agents.expense_assistant.loop import (
    _run_agent_loop,
    _subagent_event,
    run_agent as _run_agent_impl,
    run_agent_with_isolation as _run_agent_with_isolation_impl,
    _message_text,
)
from backend.agents.expense_assistant.manager_explain import compose_explanation
from backend.agents.expense_assistant.manifest import (
    AgentRole,
    EXTERNAL_EVIDENCE_TOOLS,
    MAX_EXTERNAL_EVIDENCE_TOOL_CALLS,
    RECEIPT_ANALYSER_SKILLS,
    SUBAGENT_ALLOWED_TOOLS,
    SUBAGENT_CONNECTORS,
    SUBAGENT_SKILLS,
    SUBAGENT_TOOL_MAP,
    TOOL_REGISTRY,
    TOOL_REGISTRY_READ,
    TOOL_REGISTRY_WRITE,
    _EXPENSE_AGENT_MANIFEST,
    _canonical_agent_role,
)
from backend.agents.expense_assistant.tool_defs import _TOOL_DEFS, get_tools_for_role
from backend.agents.expense_assistant import tool_handlers as _tool_handlers_mod
from backend.agents.expense_assistant.tool_handlers import (
    TOOL_HANDLERS,
    _gpt4o_ocr,
    tool_check_budget_status,
    tool_check_duplicate_invoice,
    tool_extract_receipt_fields as _tool_extract_receipt_fields_impl,
    tool_get_budget_summary,
    tool_get_employee_submission_history,
    tool_get_my_recent_submissions,
    tool_get_pending_approval_queue,
    tool_get_policy_rules,
    tool_get_report_detail,
    tool_get_spend_summary,
    tool_get_submission_for_review,
    tool_get_team_spend_summary,
    tool_lookup_card_transaction,
    tool_lookup_ctrip_booking,
    tool_lookup_didi_trip,
    tool_suggest_category,
    tool_update_draft_field,
    tool_update_report_line_field,
)
from backend.db.store import (
    create_audit_log,
    create_draft,
    get_db,
    get_draft,
    get_report,
    update_draft_field as store_update_draft_field,
    update_draft_receipt,
)
from backend.quick.finalize import save_draft_as_report_line
from backend.services.injection_guard import scan_text
from backend.storage import get_storage

router = APIRouter()


async def run_agent_with_isolation(*args, **kwargs):
    """Backward-compatible wrapper so tests can monkeypatch chat.get_llm."""
    from backend.agents.expense_assistant import loop as loop_mod

    previous = loop_mod.get_llm
    previous_ocr = _tool_handlers_mod._gpt4o_ocr
    loop_mod.get_llm = get_llm
    _tool_handlers_mod._gpt4o_ocr = _gpt4o_ocr
    try:
        async for event in _run_agent_with_isolation_impl(*args, **kwargs):
            yield event
    finally:
        loop_mod.get_llm = previous
        _tool_handlers_mod._gpt4o_ocr = previous_ocr


async def run_agent(*args, **kwargs):
    """Backward-compatible wrapper so tests can monkeypatch chat.get_llm."""
    from backend.agents.expense_assistant import loop as loop_mod

    previous = loop_mod.get_llm
    previous_ocr = _tool_handlers_mod._gpt4o_ocr
    loop_mod.get_llm = get_llm
    _tool_handlers_mod._gpt4o_ocr = _gpt4o_ocr
    try:
        async for event in _run_agent_impl(*args, **kwargs):
            yield event
    finally:
        loop_mod.get_llm = previous
        _tool_handlers_mod._gpt4o_ocr = previous_ocr


async def tool_extract_receipt_fields(*args, **kwargs):
    """Backward-compatible wrapper so tests can monkeypatch chat._gpt4o_ocr."""
    previous_ocr = _tool_handlers_mod._gpt4o_ocr
    _tool_handlers_mod._gpt4o_ocr = _gpt4o_ocr
    try:
        return await _tool_extract_receipt_fields_impl(*args, **kwargs)
    finally:
        _tool_handlers_mod._gpt4o_ocr = previous_ocr


# ═══════════════════════════════════════════════════════════════════
# 路由 — Draft CRUD + Chat Stream + Submit
# ═══════════════════════════════════════════════════════════════════

class ChatMessageBody(BaseModel):
    message: str


class EmployeeChatBody(BaseModel):
    """Unified employee-drawer request body (stateless, multi-turn).

    Front-end maintains chat_history in memory and sends last N turns.
    `context` is optional page state — the endpoint injects it into the
    conversation so the LLM knows what report the user is looking at.
    """
    messages: list[dict]
    context: Optional[dict] = None


async def _audit_injection_attempt(
    db: AsyncSession,
    *,
    ctx: UserContext,
    text: str,
    resource_type: str,
    resource_id: Optional[str] = None,
) -> None:
    injection_report = scan_text(text)
    if not injection_report:
        return
    await create_audit_log(
        db,
        actor_id=ctx.user_id,
        action="injection_attempt",
        resource_type=resource_type,
        resource_id=resource_id,
        detail={"patterns": injection_report["patterns"]},
    )


def _draft_dict(draft) -> dict:
    return {
        "id": draft.id,
        "employee_id": draft.employee_id,
        "receipt_url": draft.receipt_url,
        "fields": draft.fields or {},
        "field_sources": draft.field_sources or {},
        "chat_history": draft.chat_history or [],
        "submitted_as": draft.submitted_as,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }


@router.post("/drafts", status_code=201)
async def create_draft_route(
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    draft = await create_draft(db, ctx.user_id)
    await create_audit_log(
        db, actor_id=ctx.user_id, action="draft_created",
        resource_type="draft", resource_id=draft.id,
        detail={},
    )
    return _draft_dict(draft)


@router.get("/drafts/{draft_id}")
async def get_draft_route(
    draft_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    draft = await get_draft(db, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft 不存在")
    if draft.employee_id != ctx.user_id and ctx.role == "employee":
        raise HTTPException(status_code=403, detail="权限不足")
    return _draft_dict(draft)


@router.post("/drafts/{draft_id}/receipt")
async def upload_receipt_to_draft(
    draft_id: str,
    receipt_image: UploadFile = File(...),
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    draft = await get_draft(db, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft 不存在")
    if draft.employee_id != ctx.user_id:
        raise HTTPException(status_code=403, detail="权限不足")

    storage = get_storage()
    receipt_url = await storage.save(receipt_image, receipt_image.filename or "receipt.jpg")
    updated = await update_draft_receipt(db, draft_id, receipt_url)
    return _draft_dict(updated)


class PatchDraftFieldBody(BaseModel):
    field: str
    value: Any
    source: str = "user"


@router.patch("/drafts/{draft_id}/field")
async def patch_draft_field(
    draft_id: str,
    body: PatchDraftFieldBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    draft = await get_draft(db, draft_id)
    if not draft or draft.employee_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="Draft 不存在")
    await store_update_draft_field(db, draft_id, body.field, body.value, body.source)
    return {"ok": True}


@router.post("/drafts/{draft_id}/message")
async def send_chat_message(
    draft_id: str,
    body: ChatMessageBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """SSE streaming — 每个事件一行 `data: {...}\\n\\n`。"""
    await _audit_injection_attempt(
        db,
        ctx=ctx,
        text=body.message,
        resource_type="draft",
        resource_id=draft_id,
    )

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in run_agent(
                body.message, draft_id, ctx, db,
                agent_role="expense_assistant",
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


@router.post("/explain/{submission_id}")
async def explain_submission(
    submission_id: str,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """嵌入式 AI 解释卡 — 经理/财务点开报销时调用，返回结构化 JSON。

    不是 SSE，不是对话 —— 单次请求 / 单次响应。这是"第三种 agent 形态"
    的关键：审批是 10 秒/单的高吞吐决策，chat drawer 会降低吞吐。
    """
    if ctx.role not in ("manager", "finance_admin"):
        raise HTTPException(status_code=403, detail="仅经理/财务可访问 AI 解释卡")
    result = await compose_explanation(submission_id, ctx, db)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/message")
async def send_employee_chat(
    body: EmployeeChatBody,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """Unified AI assistant drawer — single endpoint, role chosen by ctx.role.

    Routing: backend reads ``ctx.role`` and picks the agent_role + tool
    whitelist; the client cannot escalate by sending a role parameter.
      - employee users → ``expense_assistant`` with owner-scoped read/write
      - ``manager`` → owner-bypassed read (queue / audit_report / team
        analytics); zero write tools
      - ``finance_admin`` → same as manager but queue scoped to
        manager_approved state and team analytics is org-wide

    Security model unchanged from prior:
      - Every WRITE tool validates ownership + state INSIDE the tool
        (data-level ACL). A hallucinated tool call still can't touch
        someone else's data or an already-submitted report.
      - Tool whitelist excludes submit/approve/reject/pay entirely: AI
        never executes actions that carry legal/compliance weight.
      - Manager/finance read tools bypass ``employee_id == ctx.user_id``
        ownership check by design — that's the whole point of approver
        access — but each tool re-asserts ``ctx.role`` server-side.
    """
    # Map ctx.role → agent_role. Frontend can't override.
    if ctx.role in ("manager", "finance_admin"):
        agent_role: AgentRole = "manager"
    else:
        agent_role = "expense_assistant"

    messages_for_agent = list(body.messages or [])
    draft_id = str((body.context or {}).get("draft_id") or "").strip() or None
    for msg in reversed(messages_for_agent):
        if msg.get("role") == "user":
            await _audit_injection_attempt(
                db,
                ctx=ctx,
                text=_message_text(msg),
                resource_type="draft" if draft_id else "chat",
                resource_id=draft_id,
            )
            break

    # If the caller passed page context (e.g. {report_id: ...}), inject a
    # synthesized first user turn so the LLM knows what the user is looking
    # at. Keeps prompts short (we don't re-send this every turn; client
    # sends it once per page load).
    if body.context:
        report_id = (body.context or {}).get("report_id")
        if report_id:
            report = await get_report(db, report_id)
            # Silent if lookup fails — the user can still chat about other
            # things. ACL per tool prevents any ability to act on it.
            #
            # Visibility: employees see their own report; managers/finance
            # see any report (their job). The owner check used to drop the
            # context entirely for managers — that was the "AI 报销助手"
            # giving generic answers about someone else's high-risk report.
            is_owner = report and report.employee_id == ctx.user_id
            is_approver = report and ctx.role in ("manager", "finance_admin")
            if report and (is_owner or is_approver):
                from backend.db.store import list_report_submissions
                subs = await list_report_submissions(db, report_id)
                ctx_text = (
                    f"[当前上下文] 打开的报销单: {report.title} "
                    f"(id={report_id}, status={report.status}, {len(subs)} 笔, "
                    f"提交人={report.employee_id})\n"
                )
                if agent_role == "manager":
                    max_risk = max(
                        (float(s.risk_score) for s in subs if s.risk_score is not None),
                        default=0.0,
                    )
                    has_investigation = any(
                        (s.audit_report or {}).get("investigation") for s in subs
                    )
                    ctx_text += (
                        f"[审批视角] 最高风险分={max_risk:.0f}/100"
                        + (" · 已生成 OODA 调查报告" if has_investigation else "")
                        + "。需要详细解释为何高风险时，调用 get_submission_for_review "
                        f"读取 audit_report（含 fraud_signals + investigation）。\n"
                    )
                for i, s in enumerate(subs, 1):
                    risk_hint = (
                        f" | risk={float(s.risk_score):.0f} {s.tier or ''}"
                        if s.risk_score is not None else ""
                    )
                    ctx_text += (
                        f"  line#{i}: id={s.id} | merchant={s.merchant or '-'} | "
                        f"{s.currency or ''} {s.amount} | category={s.category or '-'} | "
                        f"date={s.date or '-'}{risk_hint}\n"
                    )
                messages_for_agent = [{"role": "user", "content": ctx_text}] + messages_for_agent

    async def event_stream() -> AsyncIterator[str]:
        try:
            if agent_role == "expense_assistant" and draft_id:
                user_message = ""
                for msg in reversed(messages_for_agent):
                    if msg.get("role") == "user":
                        user_message = str(msg.get("content") or "")
                        break
                async for event in run_agent(
                    user_message=user_message,
                    draft_id=draft_id,
                    ctx=ctx,
                    db=db,
                    agent_role=agent_role,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                async for event in run_agent(
                    user_message="",
                    draft_id=None,
                    ctx=ctx,
                    db=db,
                    agent_role=agent_role,
                    messages_history=messages_for_agent,
                ):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/drafts/{draft_id}/submit", status_code=202)
async def submit_draft(
    draft_id: str,
    background_tasks: BackgroundTasks,
    ctx: UserContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """把 draft 转正为报销单行项。"""
    sub_id, report_id = await save_draft_as_report_line(draft_id, ctx, db)
    return {
        "id": sub_id,
        "draft_id": draft_id,
        "report_id": report_id,
        "status": "in_report",
        "message": "草稿已保存到报销单，请在报销单中提交审批。",
    }
