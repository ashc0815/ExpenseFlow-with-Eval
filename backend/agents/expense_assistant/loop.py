"""Agent loop and subagent-aware trace events for the expense assistant."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext
from backend.db.store import append_draft_messages, get_draft

from .llm import _identity_answer, _is_identity_question, get_llm
from .manifest import (
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
    _canonical_agent_role,
    _subagent_for_tool,
)
from .tool_defs import _TOOL_DEFS
from .tool_handlers import TOOL_HANDLERS

def _message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _tool_output_summary(tool_name: str, result: Any) -> str:
    """Small, trace-safe summary of a tool result."""
    if not isinstance(result, dict):
        return type(result).__name__
    if result.get("error"):
        return f"error: {result.get('error')}"
    if tool_name == "extract_receipt_fields":
        missing = result.get("missing_fields") or [
            f for f in ("merchant", "amount", "date", "invoice_number")
            if not result.get(f)
        ]
        flags = result.get("risk_flags") or []
        pieces = []
        if result.get("merchant"):
            pieces.append(f"merchant={result.get('merchant')}")
        if result.get("amount") is not None:
            pieces.append(f"amount={result.get('amount')}")
        if missing:
            pieces.append(f"missing={','.join(missing)}")
        if flags:
            pieces.append(f"risk_flags={','.join(flags)}")
        return "; ".join(pieces) or "receipt fields extracted"
    if tool_name in {"lookup_didi_trip", "lookup_ctrip_booking", "lookup_card_transaction"}:
        candidates = result.get("candidates") or []
        if len(candidates) == 1:
            c = candidates[0] or {}
            amount = c.get("net_amount", c.get("amount"))
            return f"one candidate matched amount={amount} date={c.get('date') or '-'}"
        return f"{len(candidates)} candidates matched"
    if tool_name == "get_policy_rules":
        return "policy rules loaded"
    if tool_name == "check_duplicate_invoice":
        return "duplicate invoice found" if result.get("is_duplicate") else "invoice not duplicate"
    if tool_name == "suggest_category":
        return f"category={result.get('category')} confidence={result.get('confidence')}"
    if tool_name == "update_draft_field":
        return f"{result.get('field')} updated" if result.get("ok") else "draft update failed"
    return "ok"


def _external_evidence_clarification(attempts: list[dict]) -> str:
    tools = {str(a.get("tool") or "") for a in attempts}
    inputs: dict[str, Any] = {}
    for attempt in attempts:
        inp = attempt.get("input") or {}
        if isinstance(inp, dict):
            inputs.update({k: v for k, v in inp.items() if v not in (None, "")})

    needed: list[str] = []
    ctrip_needs_id = "lookup_ctrip_booking" in tools and not inputs.get("booking_id")
    didi_needs_id = "lookup_didi_trip" in tools and not (inputs.get("order_id") or inputs.get("trip_id"))
    if ctrip_needs_id and didi_needs_id:
        needed.append("携程/Trip.com订单号或滴滴行程/订单号")
    elif ctrip_needs_id:
        needed.append("携程/Trip.com订单号")
    elif didi_needs_id:
        needed.append("滴滴行程/订单号")
    if "lookup_card_transaction" in tools and inputs.get("amount") is None and not inputs.get("date"):
        needed.append("信用卡/公司卡扣款日期或金额")
    if not inputs.get("date"):
        needed.append("消费/出行日期")
    if inputs.get("amount") is None:
        needed.append("金额或扣款金额")
    if not (inputs.get("merchant_hint") or inputs.get("city")):
        needed.append("城市、酒店/航班/路线或商户关键词")
    if "lookup_ctrip_booking" in tools:
        needed.append("是否取消、改签或退款")

    seen = set()
    needed = [item for item in needed if not (item in seen or seen.add(item))]
    if not needed:
        needed = ["订单号或更精确的商户/行程信息", "是否取消、改签或退款"]

    return (
        "我还不能唯一确认这笔费用，先不自动补齐，避免把错误信息写进草稿。"
        f"请补充：{'、'.join(needed)}。"
        "你回复后，我会继续核对外部证据。"
    )


def _subagent_event(
    *,
    subagent: str,
    event: str,
    tool: Optional[str] = None,
    tool_input: Optional[dict] = None,
    output_summary: Optional[str] = None,
    decision: Optional[str] = None,
    labels: Optional[list[str]] = None,
    written_fields: Optional[list[str]] = None,
    blocked_reason: Optional[str] = None,
) -> dict:
    payload = {
        "type": "subagent_step",
        "subagent": subagent,
        "event": event,
        "allowed_tools": SUBAGENT_ALLOWED_TOOLS.get(subagent, []),
        "skills": SUBAGENT_SKILLS.get(subagent, []),
        "connectors": SUBAGENT_CONNECTORS.get(subagent, []),
    }
    if tool:
        payload["tool"] = tool
    if tool_input is not None:
        payload["input"] = tool_input
    if output_summary:
        payload["output_summary"] = output_summary
    if decision:
        payload["decision"] = decision
    if labels:
        payload["labels"] = labels
    if written_fields:
        payload["written_fields"] = written_fields
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    return payload


_EVIDENCE_UNSAFE_STATUSES = {
    "auth_error",
    "conflict",
    "malformed_response",
    "multiple_candidates",
    "not_configured",
    "not_found",
    "provider_error",
    "rate_limited",
    "timeout",
}


def _candidate_amount(candidate: dict) -> float | None:
    for key in ("net_amount", "amount"):
        value = candidate.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _candidate_has_lifecycle_blocker(candidate: dict) -> bool:
    status = str(candidate.get("status") or "").lower()
    if status in {"cancelled", "canceled", "refunded", "rebooked", "void"}:
        return True
    try:
        refund = float(candidate.get("refund_amount") or 0)
    except (TypeError, ValueError):
        refund = 0.0
    return refund > 0


def _write_gate_decision(evidence_attempts: list[dict], deferred_writes: list[dict]) -> tuple[bool, str]:
    """Server-side writer gate for reimbursement completion.

    Prompts help, but the write boundary should not rely on model discipline:
    provider completion needs unique evidence, Didi/Ctrip claims need card
    cross-checks, and lifecycle/conflict cases must stop before draft writes.
    """
    if not deferred_writes:
        return True, "no_writes"

    tools_seen = {str(a.get("tool") or "") for a in evidence_attempts}
    sources = " ".join(
        str((w.get("input") or {}).get("source") or "")
        for w in deferred_writes
    ).lower()
    provider_claim = bool({"lookup_didi_trip", "lookup_ctrip_booking"} & tools_seen) or any(
        marker in sources for marker in ("didi", "ctrip")
    )

    candidates_by_tool: dict[str, list[dict]] = {}
    for attempt in evidence_attempts:
        tool = str(attempt.get("tool") or "")
        result = attempt.get("result") or {}
        if not isinstance(result, dict):
            return False, "external_evidence_not_structured"
        status = str(result.get("status") or "").lower()
        candidates = result.get("candidates") or []
        if result.get("error") or status in _EVIDENCE_UNSAFE_STATUSES:
            return False, f"{tool}_status_{status or 'error'}"
        if tool in EXTERNAL_EVIDENCE_TOOLS and len(candidates) != 1:
            return False, f"{tool}_not_unique"
        if any(_candidate_has_lifecycle_blocker(c or {}) for c in candidates):
            return False, f"{tool}_lifecycle_or_refund"
        candidates_by_tool[tool] = [c or {} for c in candidates]

    if provider_claim and "lookup_card_transaction" not in candidates_by_tool:
        return False, "provider_completion_requires_card_transaction"

    provider_amounts: list[float] = []
    for tool in ("lookup_didi_trip", "lookup_ctrip_booking"):
        provider_amounts.extend(
            amount
            for amount in (_candidate_amount(c) for c in candidates_by_tool.get(tool, []))
            if amount is not None
        )
    card_amounts = [
        amount
        for amount in (_candidate_amount(c) for c in candidates_by_tool.get("lookup_card_transaction", []))
        if amount is not None
    ]
    if provider_amounts and card_amounts:
        if all(abs(p - c) > 0.01 for p in provider_amounts for c in card_amounts):
            return False, "provider_card_amount_mismatch"

    provider_dates = {
        str(c.get("date"))
        for tool in ("lookup_didi_trip", "lookup_ctrip_booking")
        for c in candidates_by_tool.get(tool, [])
        if c.get("date")
    }
    card_dates = {
        str(c.get("date"))
        for c in candidates_by_tool.get("lookup_card_transaction", [])
        if c.get("date")
    }
    if provider_dates and card_dates and provider_dates.isdisjoint(card_dates):
        return False, "provider_card_date_mismatch"

    return True, "can_write_draft"


def _write_gate_user_message(reason: str) -> str:
    if "requires_card_transaction" in reason:
        return "我还需要核对对应的信用卡/公司卡扣款后才能写入草稿。请补充扣款日期、金额或卡交易线索。"
    if "multiple_candidates" in reason or "not_unique" in reason:
        return "我找到多笔可能匹配的记录，不能替你猜是哪一笔。请补充订单号、行程号、具体时间或路线。"
    if "amount_mismatch" in reason:
        return "外部证据的金额不一致，我先不写入。请确认实际报销金额或提供正确订单。"
    if "date_mismatch" in reason:
        return "外部证据的日期不一致，我先不写入。请确认正确消费日期或提供正确订单。"
    if "lifecycle_or_refund" in reason:
        return "这笔费用涉及取消、退款或改签，我先不写入。请确认是否仍有可报销的净支出。"
    return "当前证据还不完整，我先不写入草稿。请补充订单号、日期、金额、城市/路线或商户信息。"


async def _run_agent_loop(
    user_message: str,
    draft_id: Optional[str],
    ctx: UserContext,
    db: AsyncSession,
    agent_role: str = "expense_assistant",
    messages_history: Optional[list[dict]] = None,
    extra_handlers: Optional[dict] = None,
    allowed_tool_names_override: Optional[set[str]] = None,
    defer_write_tools: bool = False,
) -> AsyncIterator[dict]:
    """流式跑 agent，每个事件 yield 一个 dict 给前端。

    两种模式：
      - draft_id 非空（submit 模式）：从 draft.chat_history 读历史，
        loop 结束后把本轮新消息 append 回 DB。
      - draft_id 为 None（stateless QA 模式）：调用方通过 messages_history
        传入完整历史（前端内存维护），后端不持久化任何东西。

    agent_role 决定 tool 白名单（TOOL_REGISTRY）。run_agent 会把 role
    对应的 tool 定义喂给 LLM，并在 dispatch 前再校验一次——即使 LLM
    被 prompt injection 幻觉出白名单外的 tool 名，也会被拒绝执行。

    事件类型：
      - {type: "message_start"}
      - {type: "assistant_text", text: "..."}
      - {type: "tool_call", name, input, id}
      - {type: "tool_result", id, result}
      - {type: "draft_updated", fields, field_sources}
      - {type: "message_end", stop_reason}
      - {type: "error", message}
    """
    agent_role = _canonical_agent_role(agent_role)
    error_role = "employee" if ctx.role == "employee" and agent_role == "expense_assistant" else agent_role
    allowed_tool_names = set(
        allowed_tool_names_override
        if allowed_tool_names_override is not None
        else TOOL_REGISTRY.get(agent_role, [])
    )
    if draft_id is None:
        allowed_tool_names -= {
            "extract_receipt_fields",
            "suggest_category",
            "check_duplicate_invoice",
            "update_draft_field",
            "check_budget_status",
        }
    tool_order = TOOL_REGISTRY.get(agent_role, [])
    if allowed_tool_names_override is not None:
        tool_order = [name for name in tool_order if name in allowed_tool_names]
        tool_order += [
            name for name in allowed_tool_names
            if name in _TOOL_DEFS and name not in tool_order
        ]
    tools_for_llm = [
        _TOOL_DEFS[name]
        for name in tool_order
        if name in _TOOL_DEFS and name in allowed_tool_names
    ]

    # ── 根据模式加载消息历史 ──
    messages: list[dict]
    new_messages_to_persist: Optional[list[dict]]
    if draft_id is not None:
        # Submit 模式——从 draft 读历史，跑完写回 DB
        draft = await get_draft(db, draft_id)
        if not draft:
            yield {"type": "error", "message": "Draft not found"}
            return
        if draft.employee_id != ctx.user_id:
            yield {"type": "error", "message": "权限不足"}
            return
        draft_ctx = {
            "role": "user",
            "content": (
                f"[当前草稿上下文] draft_id={draft_id} "
                f"receipt_uploaded={'true' if draft.receipt_url else 'false'} "
                f"fields={json.dumps(draft.fields or {}, ensure_ascii=False)}"
            ),
        }
        messages = [draft_ctx] + list(draft.chat_history or [])
        new_user_msg = {"role": "user", "content": user_message}
        messages.append(new_user_msg)
        new_messages_to_persist = [new_user_msg]
    else:
        # Stateless QA 模式——调用方全权管理历史，后端不持久化
        messages = list(messages_history or [])
        new_messages_to_persist = None

    latest_user_text = user_message
    if not latest_user_text:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                latest_user_text = _message_text(msg)
                break

    yield {"type": "message_start"}
    if agent_role == "expense_assistant" and _is_identity_question(latest_user_text):
        yield {"type": "assistant_text", "text": _identity_answer()}
        yield {"type": "message_end", "stop_reason": "end_turn"}
        return

    llm = get_llm()
    subagent_trace_steps: list[dict] = []
    evidence_attempts_this_turn: list[dict] = []
    write_tool_names = set(TOOL_REGISTRY_WRITE.get(agent_role, []))

    async def _emit_trace_end(stop_reason: str) -> AsyncIterator[dict]:
        if subagent_trace_steps:
            yield {
                "type": "agent_trace",
                "agent": "receipt-analysis-orchestrator",
                "skills": RECEIPT_ANALYSER_SKILLS,
                "subagents": {
                    name: {
                        "allowed_tools": tools,
                        "skills": SUBAGENT_SKILLS.get(name, []),
                        "connectors": SUBAGENT_CONNECTORS.get(name, []),
                    }
                    for name, tools in SUBAGENT_ALLOWED_TOOLS.items()
                },
                "steps": subagent_trace_steps,
                "draft_id": draft_id,
            }
        yield {"type": "message_end", "stop_reason": stop_reason}

    # Agent loop — 最多 10 轮防爆
    for _ in range(10):
        response = await llm.next_turn(messages, tools_for_llm, agent_role=agent_role)

        if response.text:
            yield {"type": "assistant_text", "text": response.text}

        # 构造 assistant turn（包含 text + tool_use blocks）
        assistant_content: list[dict] = []
        if response.text:
            assistant_content.append({"type": "text", "text": response.text})
        if response.reasoning_content:
            assistant_content.append({
                "type": "reasoning_content",
                "text": response.reasoning_content,
            })
        for tc in response.tool_calls:
            assistant_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
        if assistant_content:
            assistant_msg = {"role": "assistant", "content": assistant_content}
            messages.append(assistant_msg)
            if new_messages_to_persist is not None:
                new_messages_to_persist.append(assistant_msg)

        if response.stop_reason == "end_turn" and defer_write_tools:
            deferred_from_text = _extract_deferred_writes_from_text(response.text, draft_id)
            if deferred_from_text:
                async for ev in _execute_deferred_writes(
                    deferred_from_text,
                    ctx=ctx,
                    db=db,
                    draft_id=draft_id,
                    extra_handlers=extra_handlers,
                    subagent_trace_steps=subagent_trace_steps,
                    evidence_attempts=evidence_attempts_this_turn,
                ):
                    yield ev
                async for ev in _emit_trace_end("end_turn"):
                    yield ev
                break

        if response.stop_reason == "end_turn":
            async for ev in _emit_trace_end("end_turn"):
                yield ev
            break

        # 执行工具
        if response.stop_reason == "tool_use" and response.tool_calls:
            tool_results_content: list[dict] = []
            draft_changed = False
            written_fields: list[str] = []
            deferred_writes: list[dict] = []
            for tc in response.tool_calls:
                subagent = _subagent_for_tool(tc["name"])
                call_step = _subagent_event(
                    subagent=subagent,
                    event="tool_call",
                    tool=tc["name"],
                    tool_input=tc["input"],
                )
                subagent_trace_steps.append(call_step)
                yield call_step
                yield {
                    "type": "tool_call",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["input"],
                    "subagent": subagent,
                    "skills": SUBAGENT_SKILLS.get(subagent, []),
                    "connectors": SUBAGENT_CONNECTORS.get(subagent, []),
                }
                is_deferred_write = (
                    defer_write_tools
                    and tc["name"] in write_tool_names
                    and (draft_id is not None or tc["name"] == "update_report_line_field")
                )
                # 白名单强制——防 prompt injection 的最后一道闸。In isolated
                # expense-assistant mode, write tool calls are captured as a
                # structured plan and executed later by the writer phase.
                if is_deferred_write:
                    result = {
                        "ok": True,
                        "deferred_to": "draft-writer",
                        "field": tc.get("input", {}).get("field"),
                        "tool": tc["name"],
                    }
                    deferred_writes.append({
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc.get("input") or {},
                    })
                elif tc["name"] not in allowed_tool_names:
                    result = {
                        "error": f"tool '{tc['name']}' not allowed for role '{error_role}'",
                        "allowed": sorted(allowed_tool_names),
                    }
                else:
                    handler = (extra_handlers or {}).get(tc["name"]) or TOOL_HANDLERS.get(tc["name"])
                    if not handler:
                        result = {"error": f"unknown tool {tc['name']}"}
                    else:
                        try:
                            result = await handler(tc["input"], ctx, db, draft_id)
                        except Exception as e:  # noqa: BLE001
                            result = {"error": str(e)}
                output_summary = _tool_output_summary(tc["name"], result)
                result_step = _subagent_event(
                    subagent=subagent,
                    event="tool_result",
                    tool=tc["name"],
                    tool_input=tc["input"],
                    output_summary=output_summary,
                )
                subagent_trace_steps.append(result_step)
                if not is_deferred_write:
                    yield {
                        "type": "tool_result",
                        "id": tc["id"],
                        "name": tc["name"],
                        "result": result,
                        "subagent": subagent,
                        "connectors": SUBAGENT_CONNECTORS.get(subagent, []),
                        "output_summary": output_summary,
                    }
                yield result_step
                if (
                    tc["name"] == "update_draft_field"
                    and result.get("ok")
                    and not is_deferred_write
                ):
                    draft_changed = True
                    if result.get("field"):
                        written_fields.append(str(result["field"]))
                if tc["name"] in EXTERNAL_EVIDENCE_TOOLS:
                    evidence_attempts_this_turn.append({
                        "tool": tc["name"],
                        "input": tc["input"],
                        "result": result,
                    })
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            if draft_changed and draft_id is not None:
                # 推一条"draft 已更新"事件给前端，让左侧表单实时同步
                fresh = await get_draft(db, draft_id)
                write_step = _subagent_event(
                    subagent="draft-writer",
                    event="draft_updated",
                    written_fields=written_fields,
                )
                subagent_trace_steps.append(write_step)
                yield write_step
                yield {
                    "type": "draft_updated",
                    "fields": fresh.fields or {},
                    "field_sources": fresh.field_sources or {},
                }

            tool_user_msg = {"role": "user", "content": tool_results_content}
            messages.append(tool_user_msg)
            if new_messages_to_persist is not None:
                new_messages_to_persist.append(tool_user_msg)

            if deferred_writes:
                async for ev in _execute_deferred_writes(
                    deferred_writes,
                    ctx=ctx,
                    db=db,
                    draft_id=draft_id,
                    extra_handlers=extra_handlers,
                    subagent_trace_steps=subagent_trace_steps,
                    evidence_attempts=evidence_attempts_this_turn,
                ):
                    yield ev
                async for ev in _emit_trace_end("end_turn"):
                    yield ev
                break

            if (
                len(evidence_attempts_this_turn) >= MAX_EXTERNAL_EVIDENCE_TOOL_CALLS
                and not draft_changed
            ):
                clarification = _external_evidence_clarification(evidence_attempts_this_turn)
                assistant_msg = {"role": "assistant", "content": [{"type": "text", "text": clarification}]}
                messages.append(assistant_msg)
                if new_messages_to_persist is not None:
                    new_messages_to_persist.append(assistant_msg)
                yield {"type": "assistant_text", "text": clarification}
                async for ev in _emit_trace_end("needs_user_clarification"):
                    yield ev
                break
            continue  # 下一轮 LLM

        # 未知 stop_reason
        async for ev in _emit_trace_end(response.stop_reason):
            yield ev
        break
    else:
        # 走到 for 的 else 说明达到 10 轮上限
        yield {"type": "error", "message": "Agent loop exceeded 10 iterations"}

    # 持久化新消息到 draft.chat_history（QA stateless 模式跳过）
    if draft_id is not None and new_messages_to_persist:
        await append_draft_messages(db, draft_id, new_messages_to_persist)


def _extract_deferred_writes_from_text(text: str, draft_id: Optional[str]) -> list[dict]:
    """Parse a reader-produced JSON action plan into writer calls."""
    if not text or "field_updates" not in text:
        return []

    candidates = [text.strip()]
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if match:
        candidates.insert(0, match.group(1))
    brace = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if brace:
        candidates.append(brace.group(0))

    payload = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    if not isinstance(payload, dict):
        return []
    updates = payload.get("field_updates")
    if not isinstance(updates, list):
        return []

    calls: list[dict] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        tool_name = str(
            update.pop("tool", "")
            or ("update_draft_field" if draft_id else "update_report_line_field")
        )
        if tool_name not in {"update_draft_field", "update_report_line_field"}:
            continue
        inp = dict(update)
        if tool_name == "update_draft_field" and "source" not in inp:
            inp["source"] = "agent_verified"
        if not inp.get("field") or "value" not in inp:
            continue
        calls.append({
            "id": f"writer_{uuid.uuid4().hex[:12]}",
            "name": tool_name,
            "input": inp,
        })
    return calls


async def _execute_deferred_writes(
    deferred_writes: list[dict],
    *,
    ctx: UserContext,
    db: AsyncSession,
    draft_id: Optional[str],
    extra_handlers: Optional[dict],
    subagent_trace_steps: list[dict],
    evidence_attempts: list[dict],
) -> AsyncIterator[dict]:
    """Execute writer-phase tool calls using only the structured action plan."""
    written_fields: list[str] = []
    draft_changed = False
    can_write, reason = _write_gate_decision(evidence_attempts, deferred_writes)
    if not can_write:
        blocked_msg = _write_gate_user_message(reason)
        blocked_step = _subagent_event(
            subagent="draft-writer",
            event="write_blocked",
            decision="blocked",
            blocked_reason=reason,
        )
        subagent_trace_steps.append(blocked_step)
        yield blocked_step
        yield {
            "type": "writer_gate_blocked",
            "subagent": "draft-writer",
            "blocked_reason": reason,
            "text": blocked_msg,
        }
        yield {"type": "assistant_text", "text": blocked_msg}
        return

    for call in deferred_writes:
        name = call.get("name")
        inp = call.get("input") or {}
        call_id = call.get("id") or f"writer_{uuid.uuid4().hex[:12]}"
        subagent = "draft-writer"
        call_step = _subagent_event(
            subagent=subagent,
            event="tool_call",
            tool=name,
            tool_input=inp,
            decision="writer_phase",
        )
        subagent_trace_steps.append(call_step)
        yield call_step
        yield {
            "type": "tool_call",
            "id": call_id,
            "name": name,
            "input": inp,
            "subagent": subagent,
            "skills": SUBAGENT_SKILLS.get(subagent, []),
            "connectors": SUBAGENT_CONNECTORS.get(subagent, []),
            "phase": "writer",
        }

        if name not in {"update_draft_field", "update_report_line_field"}:
            result = {"error": f"writer tool '{name}' is not allowed"}
        else:
            handler = (extra_handlers or {}).get(name) or TOOL_HANDLERS.get(name)
            if not handler:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = await handler(inp, ctx, db, draft_id)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}

        output_summary = _tool_output_summary(str(name), result)
        result_step = _subagent_event(
            subagent=subagent,
            event="tool_result",
            tool=name,
            tool_input=inp,
            output_summary=output_summary,
            decision="writer_phase",
        )
        subagent_trace_steps.append(result_step)
        yield {
            "type": "tool_result",
            "id": call_id,
            "name": name,
            "result": result,
            "subagent": subagent,
            "connectors": SUBAGENT_CONNECTORS.get(subagent, []),
            "output_summary": output_summary,
            "phase": "writer",
        }
        yield result_step

        if name == "update_draft_field" and result.get("ok"):
            draft_changed = True
            if result.get("field"):
                written_fields.append(str(result["field"]))

    if draft_changed and draft_id is not None:
        fresh = await get_draft(db, draft_id)
        write_step = _subagent_event(
            subagent="draft-writer",
            event="draft_updated",
            written_fields=written_fields,
            decision="writer_phase",
        )
        subagent_trace_steps.append(write_step)
        yield write_step
        yield {
            "type": "draft_updated",
            "fields": fresh.fields or {},
            "field_sources": fresh.field_sources or {},
        }


async def run_agent_with_isolation(
    user_message: str,
    draft_id: Optional[str],
    ctx: UserContext,
    db: AsyncSession,
    agent_role: str = "expense_assistant",
    messages_history: Optional[list[dict]] = None,
    extra_handlers: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """Expense assistant isolation: read/reason first, write from a plan only."""
    read_tools = set(TOOL_REGISTRY_READ.get(_canonical_agent_role(agent_role), []))
    async for event in _run_agent_loop(
        user_message=user_message,
        draft_id=draft_id,
        ctx=ctx,
        db=db,
        agent_role=agent_role,
        messages_history=messages_history,
        extra_handlers=extra_handlers,
        allowed_tool_names_override=read_tools,
        defer_write_tools=True,
    ):
        yield event


async def run_agent(
    user_message: str,
    draft_id: Optional[str],
    ctx: UserContext,
    db: AsyncSession,
    agent_role: str = "expense_assistant",
    messages_history: Optional[list[dict]] = None,
    extra_handlers: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """Public agent entrypoint. Employee assistant uses read/write isolation."""
    canonical_role = _canonical_agent_role(agent_role)
    if canonical_role == "expense_assistant":
        async for event in run_agent_with_isolation(
            user_message=user_message,
            draft_id=draft_id,
            ctx=ctx,
            db=db,
            agent_role=canonical_role,
            messages_history=messages_history,
            extra_handlers=extra_handlers,
        ):
            yield event
        return

    async for event in _run_agent_loop(
        user_message=user_message,
        draft_id=draft_id,
        ctx=ctx,
        db=db,
        agent_role=canonical_role,
        messages_history=messages_history,
        extra_handlers=extra_handlers,
    ):
        yield event
