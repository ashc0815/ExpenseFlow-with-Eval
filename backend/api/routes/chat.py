"""Agent Chat API — 员工和 AI 助手对话式报销。

架构：
  1. 真实的 Tool 定义（Anthropic schema）+ Tool 实现（操作 Draft 表）
  2. LLM 抽象层：MockLLM（规则脚本）/ RealLLM（Anthropic API，预留）
  3. 真实的 Agent Loop（while 循环 + 消息历史）
  4. SSE Streaming 端点，消息逐条推送给前端

数据流：
  POST /api/chat/drafts                       新建 draft
  POST /api/chat/drafts/{id}/receipt          上传发票到 draft
  POST /api/chat/drafts/{id}/message (SSE)    发消息给 agent，SSE 流式返回
  POST /api/chat/drafts/{id}/submit           转正为正式 submission（走审批流）
  GET  /api/chat/drafts/{id}                  读 draft 当前状态

权限边界：
  ✅ Agent 可以：读发票、推荐类别、查重、读历史、写 draft 字段
  ❌ Agent 不可以：提交 submission、修改已提交数据、调用审批
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext, require_auth
from backend.api.routes.admin import _POLICY
from backend.api.routes.submissions import _run_pipeline, _sub_dict
from backend.quick.finalize import save_draft_as_report_line
from backend.db.store import (
    append_draft_messages, create_audit_log, create_draft, create_submission,
    get_db, get_draft, get_employee, get_report, get_submission,
    get_submission_by_invoice, list_submissions, mark_draft_submitted,
    update_draft_field as store_update_draft_field,
    update_draft_receipt,
)
from backend.services.config_loader import load_prompt
from backend.storage import get_storage

router = APIRouter()

# ═══════════════════════════════════════════════════════════════════
# Tool 定义 — Anthropic schema 格式（将来直接喂给 Claude API）
#
# 架构：tool 定义集中在 _TOOL_DEFS，按 name 索引；TOOL_REGISTRY 把每个
# agent role 映射到它被允许调用的 tool 名字列表。这是防 prompt injection
# 的架构基石——LLM 只能"看到"白名单内的 tool，run_agent 在 dispatch
# 前还会二次校验，任何试图调用白名单外 tool 的请求都会被拒绝。
# ═══════════════════════════════════════════════════════════════════

AgentRole = Literal["employee_submit", "employee", "manager_explain", "manager"]

_TOOL_DEFS: dict[str, dict] = {
    "extract_receipt_fields": {
        "name": "extract_receipt_fields",
        "description": "使用 Vision 识别当前 draft 的发票图片，抽取商户/金额/日期/发票号/税额等字段。只能在 draft 已有 receipt_url 时调用。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "suggest_category": {
        "name": "suggest_category",
        "description": "根据商户名称推荐费用类别（meal/transport/accommodation/entertainment/other）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "商户名称"},
            },
            "required": ["merchant"],
        },
    },
    "check_duplicate_invoice": {
        "name": "check_duplicate_invoice",
        "description": "检查发票号是否已被该公司其他员工报销过。",
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_number": {"type": "string"},
            },
            "required": ["invoice_number"],
        },
    },
    "get_my_recent_submissions": {
        "name": "get_my_recent_submissions",
        "description": "获取当前员工最近 5 笔报销记录，用于判断消费模式或异常。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_report_detail": {
        "name": "get_report_detail",
        "description": "读取当前员工某一笔历史报销单的完整字段（商户、金额、类别、状态、日期、审批人备注等）。仅能读取自己的报销单。",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_id": {"type": "string", "description": "报销单 ID（可以是完整 uuid 或前 8 位短 ID）"},
            },
            "required": ["report_id"],
        },
    },
    "get_submission_for_review": {
        "name": "get_submission_for_review",
        "description": "（仅经理/财务可用）读取某一笔报销单的完整数据，包括 5-skill 审核报告 audit_report、风险分、tier。不做 owner 校验，因为审批角色本来就需要看别人的报销。",
        "input_schema": {
            "type": "object",
            "properties": {
                "submission_id": {"type": "string"},
            },
            "required": ["submission_id"],
        },
    },
    "get_employee_submission_history": {
        "name": "get_employee_submission_history",
        "description": "（仅经理/财务可用）查询某员工最近 N 笔历史报销，用于判断消费节奏/异常模式。返回金额、类别、日期、状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string"},
                "limit": {"type": "integer", "description": "返回笔数，默认 10"},
            },
            "required": ["employee_id"],
        },
    },
    "get_spend_summary": {
        "name": "get_spend_summary",
        "description": "聚合当前员工在指定周期内的报销金额（统一折算为 CNY），按 category 分组。返回每笔的原始币种和金额以及 CNY 等值。",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["month", "quarter"],
                    "description": "month=当前自然月，quarter=当前自然季度",
                },
            },
            "required": ["period"],
        },
    },
    "update_draft_field": {
        "name": "update_draft_field",
        "description": "更新当前 draft 的某个字段。允许的字段：merchant, amount, date, category, tax_amount, invoice_number, invoice_code, project_code, description。注意：这只是草稿，不会提交。",
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "value": {"type": "string", "description": "字段值（数字也以字符串传入）"},
                "source": {
                    "type": "string",
                    "description": "来源：ocr / agent_suggested / user_confirmed",
                    "enum": ["ocr", "agent_suggested", "user_confirmed"],
                },
            },
            "required": ["field", "value", "source"],
        },
    },
    "check_budget_status": {
        "name": "check_budget_status",
        "description": "查询当前成本中心的预算使用情况，以及提交指定金额后的预计占用比例。在员工填写金额后调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "cost_center": {
                    "type": "string",
                    "description": "员工所属成本中心编码，例如 'ENG-TRAVEL'",
                },
                "amount": {
                    "type": "number",
                    "description": "本次报销金额（人民币）",
                },
            },
            "required": ["cost_center", "amount"],
        },
    },
    "get_budget_summary": {
        "name": "get_budget_summary",
        "description": "获取当前用户所属成本中心的预算快照，用于页面加载时主动推送预算状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "description": "期间，例如 '2026-Q2'。不传时默认当前季度。",
                },
            },
            "required": [],
        },
    },
    "update_report_line_field": {
        "name": "update_report_line_field",
        "description": "修改已存在的报销单行项目的字段。根据用户消息中的行项目上下文，传入对应的 line_id。",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "string", "description": "要修改的行项目 ID（从上下文中获取）"},
                "field": {"type": "string", "description": "字段名：merchant/amount/category/date/tax_amount/invoice_number/invoice_code/project_code/description/currency"},
                "value": {"type": "string", "description": "新值（数字也以字符串传入）。类别映射：餐饮=meal、交通=transport、住宿=accommodation、招待=entertainment、其他=other"},
            },
            "required": ["line_id", "field", "value"],
        },
    },
    "get_policy_rules": {
        "name": "get_policy_rules",
        "description": "获取公司报销政策规则：费用类别、限额标准（按城市等级×员工等级）、发票要求、付款规则等。员工问报销政策相关问题时调用。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "get_pending_approval_queue": {
        "name": "get_pending_approval_queue",
        "description": "（仅经理/财务可用）获取当前角色的待审报销单队列。manager 角色返回 status=pending 的报销单；finance_admin 返回 status=manager_approved 的。每条带：employee、行数、合计金额、最高风险分、最差 tier、提交后等待天数。",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回前 N 条，按等待时间降序，默认 20"},
                "min_risk_score": {"type": "number", "description": "可选：只返回最高风险分 ≥ 该值的，用于'我那些高风险的'类问题"},
            },
            "required": [],
        },
    },
    "get_team_spend_summary": {
        "name": "get_team_spend_summary",
        "description": "（仅经理/财务可用）某 department 在指定 period 内的报销聚合：总额（CNY）、笔数、按 category 分组、top-5 员工。manager 默认本部门；finance 可查任意 department，不传 department 则返回全公司。",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["month", "quarter"], "description": "month=当前自然月，quarter=当前自然季度"},
                "department": {"type": "string", "description": "部门名（可选）。manager 即便传了也会被忽略，强制本部门。"},
                "category": {"type": "string", "description": "可选 category 过滤：meal/transport/accommodation/entertainment/other"},
            },
            "required": ["period"],
        },
    },
}

TOOL_REGISTRY: dict[str, list[str]] = {
    # ── employee_submit ──────────────────────────────────────────────
    # The quick.html inline chat agent. Owns the draft-filling flow
    # (receipt OCR → field write → budget check). Persists chat_history
    # on the draft record. Not used by the shared drawer.
    "employee_submit": [
        "extract_receipt_fields",
        "suggest_category",
        "check_duplicate_invoice",
        "get_my_recent_submissions",
        "update_draft_field",
        "check_budget_status",
    ],
    # ── employee ─────────────────────────────────────────────────────
    # The Concur/Expensify-style unified drawer agent. Runs on every
    # employee page via /api/chat/message. Security model:
    # - All WRITE tools validate ownership + state INSIDE the tool
    #   (data-level ACL, not role-level)
    # - AI has NO submit / approve / reject / pay tools — those are
    #   UI-only actions because they carry legal/compliance weight
    # Same drawer whether user is "just an employee" or also a manager;
    # the tool set is static, per-object auth does the gating.
    "employee": [
        "get_my_recent_submissions",
        "get_report_detail",
        "get_spend_summary",
        "get_budget_summary",
        "get_policy_rules",
        "update_report_line_field",
    ],
    # ── manager_explain ──────────────────────────────────────────────
    # Behind the structured AI explanation card on /manager/queue and
    # /finance/review (POST /api/chat/explain/{id}). Not a chat — single
    # request / single structured JSON response. Read-only.
    "manager_explain": [
        "get_submission_for_review",
        "get_employee_submission_history",
    ],
    # ── manager ──────────────────────────────────────────────────────
    # Multi-turn drawer chat for managers/finance reviewing reports. Same
    # /api/chat/message entrypoint as `employee` — backend picks role from
    # ctx.role, never trusts the client. Read-only by design: approve /
    # reject / pay are still UI-only (legal/compliance weight).
    # Tool surface mirrors what Ramp Copilot / Concur Joule expose to
    # approvers — single-submission drill-down + queue-level analytics.
    "manager": [
        "get_submission_for_review",
        "get_employee_submission_history",
        "get_pending_approval_queue",
        "get_team_spend_summary",
        "get_policy_rules",
    ],
}


def get_tools_for_role(role: str) -> list[dict]:
    """返回指定 role 允许使用的 tool 定义列表（喂给 LLM 的 tools 参数）。"""
    names = TOOL_REGISTRY.get(role, [])
    return [_TOOL_DEFS[n] for n in names if n in _TOOL_DEFS]

_ALLOWED_FIELDS = {
    "merchant", "amount", "date", "category", "tax_amount",
    "invoice_number", "invoice_code", "project_code", "description",
    "currency",
}

# ═══════════════════════════════════════════════════════════════════
# Tool 实现 — 真实操作 DB / 文件
# ═══════════════════════════════════════════════════════════════════

async def _gpt4o_ocr(receipt_url: str) -> Optional[dict]:
    """GPT-4o Vision 识别发票图片，返回字段 dict 或 None（失败时回退 mock）。

    receipt_url 形如 /uploads/YYYY-MM/uuid_name.jpg（LocalStorage 格式）。
    Records an LLM trace on every attempt (success or failure).
    """
    import base64
    from openai import AsyncOpenAI
    from backend.services.trace import record_trace, TraceTimer

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    # 还原文件路径：项目根 / uploads / YYYY-MM / xxx.jpg
    root = Path(__file__).resolve().parents[3]
    file_path = root / receipt_url.lstrip("/")
    if not file_path.exists():
        return None

    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return None  # GPT-4o Vision 不直接支持 PDF
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg")

    with open(file_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()

    client = AsyncOpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    user_text = (
        "Identify this receipt or invoice (any language/format) and extract fields as JSON. "
        "Set unrecognizable fields to null:\n"
        '{"merchant":"store or seller name","amount":total number,"date":"YYYY-MM-DD",'
        '"currency":"3-letter code e.g. USD/CNY/AUD","tax_amount":tax number,'
        '"invoice_number":"receipt or invoice number",'
        '"invoice_code":"invoice code (Chinese fapiao only, else null)",'
        '"seller_tax_id":"seller tax ID if present",'
        '"description":"items or services purchased",'
        '"category":"one of: 餐饮, 交通, 住宿, 办公用品, 通讯, 其他"}\n'
        "Return ONLY the JSON object, no explanation."
    )
    trace_prompt = [
        {"role": "user", "content": f"{user_text}\n<image: {receipt_url} mime={mime}>"},
    ]

    raw = ""
    parsed: Optional[dict] = None
    err: Optional[str] = None
    usage: Optional[dict] = None
    timer = TraceTimer()
    try:
        with timer:
            resp = await client.chat.completions.create(
                model=model,
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": user_text},
                    ],
                }],
            )
        raw = resp.choices[0].message.content or ""
        if getattr(resp, "usage", None):
            usage = {"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens}
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        await record_trace(
            component="ocr", model=model, prompt=trace_prompt,
            response=None, latency_ms=timer.elapsed_ms or None, error=err,
        )
        return None

    content = raw
    if "```" in content:
        for part in content.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                content = part
                break
    try:
        parsed = json.loads(content)
        parsed["_mock"] = False
        parsed["_source"] = "gpt-4o-vision"
        return parsed
    except (json.JSONDecodeError, ValueError) as exc:
        err = f"JSON parse: {exc}"
        return None
    finally:
        await record_trace(
            component="ocr",
            model=model,
            prompt=trace_prompt,
            response=raw,
            parsed_output=parsed,
            latency_ms=timer.elapsed_ms or None,
            token_usage=usage,
            error=err,
        )


async def tool_extract_receipt_fields(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """从 draft 的发票图提取字段。

    优先使用 GPT-4o Vision（需要 OPENAI_API_KEY）；
    未设置 API Key 时返回精心设计的 mock 数据（走通全部 5-Skill 审核）。
    """
    draft = await get_draft(db, draft_id)
    if not draft or not draft.receipt_url:
        return {"error": "当前 draft 没有上传发票图片"}

    # ── GPT-4o Vision（真实 OCR）──────────────────────────────────
    if os.getenv("OPENAI_API_KEY"):
        ocr_result = await _gpt4o_ocr(draft.receipt_url)
        if ocr_result and not ocr_result.get("error"):
            return ocr_result

    # ── Mock 数据（金色路径设计，无 API Key 时使用）───────────────
    import random
    from datetime import timedelta
    invoice_number = f"{random.randint(10000000, 99999999)}"

    today = date.today()
    d = today
    while d.weekday() >= 5:  # 回退到最近工作日
        d -= timedelta(days=1)

    return {
        "merchant": "海底捞火锅 (上海南京西路店)",
        "amount": 150.00,
        "date": d.isoformat(),
        "currency": "CNY",
        "tax_amount": 9.00,
        "tax_rate": 0.06,
        "invoice_number": invoice_number,
        "invoice_code": "310012135012",
        "description": "团队午餐讨论 AI 报销项目进度及下阶段需求",
        "items": [
            {"description": "午餐套餐 A", "amount": 50},
            {"description": "午餐套餐 B", "amount": 50},
            {"description": "饮料两份", "amount": 50},
        ],
        "_mock": True,
        "_note": "MOCK 数据（未设置 OPENAI_API_KEY）。设置后将自动调用 GPT-4o Vision 识别真实发票。",
    }


async def tool_suggest_category(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """简单规则：按关键词匹配类别。"""
    merchant = (args.get("merchant") or "").lower()
    rules = [
        (["海底捞", "西贝", "餐", "咖啡", "饭", "茶", "coffee", "restaurant"], "meal"),
        (["滴滴", "出租", "高铁", "机票", "airline", "taxi", "uber"], "transport"),
        (["酒店", "宾馆", "hotel", "inn"], "accommodation"),
        (["ktv", "娱乐", "会所"], "entertainment"),
    ]
    for keywords, cat in rules:
        if any(k in merchant for k in keywords):
            return {"category": cat, "confidence": 0.92, "reason": f"匹配关键词 '{merchant}'"}
    return {"category": "other", "confidence": 0.5, "reason": "无明显关键词匹配"}


async def tool_check_duplicate_invoice(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    invoice_number = args.get("invoice_number")
    if not invoice_number:
        return {"error": "缺少 invoice_number"}
    existing = await get_submission_by_invoice(db, invoice_number)
    if existing:
        return {
            "is_duplicate": True,
            "existing_submission_id": existing.id,
            "submitted_by": existing.employee_id,
            "submitted_at": existing.created_at.isoformat() if existing.created_at else None,
        }
    return {"is_duplicate": False}


async def tool_get_my_recent_submissions(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    result = await list_submissions(
        db, employee_id=ctx.user_id, page=1, page_size=5,
    )
    return {
        "items": [
            {
                "merchant": s.merchant,
                "amount": float(s.amount),
                "category": s.category,
                "date": s.date,
                "status": s.status,
            }
            for s in result["items"]
        ],
        "total": result["total"],
    }


async def tool_get_report_detail(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：获取当前员工某一笔报销单详情。强制 owner scoping。"""
    rid = (args.get("report_id") or "").strip()
    if not rid:
        return {"error": "缺少 report_id"}

    # 支持短 ID（前 8 位）——用户常会只说前几位
    sub = await get_submission(db, rid)
    if sub is None and len(rid) < 36:
        page = await list_submissions(db, employee_id=ctx.user_id, page=1, page_size=100)
        for s in page["items"]:
            if s.id.startswith(rid):
                sub = s
                break

    if sub is None:
        return {"error": f"未找到 report_id={rid}"}
    if sub.employee_id != ctx.user_id:
        # 白名单之外的额外 owner 校验——即便 LLM 猜到别人的 id 也读不到
        return {"error": "权限不足：只能查看自己的报销单"}

    return {
        "id": sub.id,
        "status": sub.status,
        "merchant": sub.merchant,
        "amount": float(sub.amount),
        "currency": sub.currency,
        "category": sub.category,
        "date": sub.date,
        "tax_amount": float(sub.tax_amount) if sub.tax_amount is not None else None,
        "description": sub.description,
        "invoice_number": sub.invoice_number,
        "project_code": sub.project_code,
        "approver_comment": getattr(sub, "approver_comment", None),
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


async def tool_get_submission_for_review(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：经理/财务读某一笔报销的完整数据 + 审计报告。

    不做 owner 校验——审批角色本来就需要看别人的报销。这是为什么这个 tool
    只在 manager_explain 白名单里，`employee` role 拿不到它。
    """
    sub_id = (args.get("submission_id") or "").strip()
    if not sub_id:
        return {"error": "缺少 submission_id"}

    sub = await get_submission(db, sub_id)
    if sub is None and len(sub_id) < 36:
        # 短 ID 前缀匹配
        page = await list_submissions(db, page=1, page_size=200)
        for s in page["items"]:
            if s.id.startswith(sub_id):
                sub = s
                break
    if sub is None:
        return {"error": f"未找到 submission_id={sub_id}"}

    return {
        "id": sub.id,
        "employee_id": sub.employee_id,
        "status": sub.status,
        "merchant": sub.merchant,
        "amount": float(sub.amount),
        "currency": sub.currency,
        "category": sub.category,
        "date": sub.date,
        "tax_amount": float(sub.tax_amount) if sub.tax_amount is not None else None,
        "description": sub.description,
        "invoice_number": sub.invoice_number,
        "department": getattr(sub, "department", None),
        "tier": sub.tier,
        "risk_score": float(sub.risk_score) if sub.risk_score is not None else None,
        "audit_report": sub.audit_report or {},
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


async def tool_get_employee_submission_history(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：经理/财务读某员工最近 N 笔报销历史，判断消费模式/异常。"""
    emp_id = (args.get("employee_id") or "").strip()
    limit = int(args.get("limit") or 10)
    if not emp_id:
        return {"error": "缺少 employee_id"}

    page = await list_submissions(db, employee_id=emp_id, page=1, page_size=limit)
    items = []
    total_amount = 0.0
    by_cat: dict[str, dict] = {}
    for s in page["items"]:
        amt = float(s.amount)
        items.append({
            "id": s.id[:8],
            "merchant": s.merchant,
            "amount": amt,
            "category": s.category,
            "date": s.date,
            "status": s.status,
        })
        total_amount += amt
        b = by_cat.setdefault(s.category or "other", {"category": s.category, "amount": 0.0, "count": 0})
        b["amount"] += amt
        b["count"] += 1

    return {
        "employee_id": emp_id,
        "items": items,
        "total_count": page["total"],
        "shown_count": len(items),
        "total_amount": round(total_amount, 2),
        "by_category": [
            {**b, "amount": round(b["amount"], 2)} for b in by_cat.values()
        ],
    }


async def tool_get_spend_summary(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：当前员工在 period 内的报销聚合，按 category 分组。"""
    period = args.get("period")
    if period not in ("month", "quarter"):
        return {"error": "period 必须是 'month' 或 'quarter'"}

    today = date.today()
    if period == "month":
        start = today.replace(day=1)
        label = f"{today.year}-{today.month:02d}"
    else:
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_start_month, day=1)
        label = f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    start_iso = start.isoformat()

    emp = await get_employee(db, ctx.user_id)
    home_cur = (emp.home_currency if emp and hasattr(emp, 'home_currency') and emp.home_currency else "CNY")
    from backend.services.fx_service import get_rate, convert as fx_convert

    page = await list_submissions(db, employee_id=ctx.user_id, page=1, page_size=500)
    in_range = [
        s for s in page["items"]
        if (s.date or "") >= start_iso
        or (s.created_at and s.created_at.date() >= start)
    ]

    by_cat: dict[str, dict] = {}
    total_home = 0.0
    items_detail = []
    for s in in_range:
        cat = s.category or "other"
        bucket = by_cat.setdefault(cat, {"category": cat, "amount_home": 0.0, "count": 0})
        orig_amt = float(s.amount)
        currency = s.currency or home_cur
        if s.exchange_rate is not None:
            home_amt = round(orig_amt * float(s.exchange_rate), 2)
        elif currency != home_cur:
            home_amt = fx_convert(orig_amt, currency, home_cur)
        else:
            home_amt = orig_amt
        bucket["amount_home"] += home_amt
        bucket["count"] += 1
        total_home += home_amt
        items_detail.append({
            "amount": orig_amt,
            "currency": currency,
            "amount_home": round(home_amt, 2),
            "category": cat,
            "merchant": s.merchant or "",
            "date": s.date or "",
        })

    return {
        "period": period,
        "period_label": label,
        "start_date": start_iso,
        "total_home": round(total_home, 2),
        "home_currency": home_cur,
        "count": len(in_range),
        "items": items_detail,
        "by_category": sorted(
            [{**b, "amount_home": round(b["amount_home"], 2)} for b in by_cat.values()],
            key=lambda x: x["amount_home"],
            reverse=True,
        ),
    }


async def tool_update_draft_field(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    field = args.get("field")
    value = args.get("value")
    source = args.get("source", "agent_suggested")
    if field not in _ALLOWED_FIELDS:
        return {"error": f"字段 '{field}' 不允许被 agent 修改", "allowed": sorted(_ALLOWED_FIELDS)}
    # 类型转换：amount / tax_amount 转 float
    if field in ("amount", "tax_amount"):
        try:
            value = float(value)
        except (ValueError, TypeError):
            return {"error": f"{field} 必须是数字"}
    await store_update_draft_field(db, draft_id, field, value, source=source)
    return {"ok": True, "field": field, "value": value, "source": source}


async def tool_update_report_line_field(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """修改某条行项目（submission）的某个字段——Concur 式 data-level ACL。

    所有安全校验在**工具内部**执行，不依赖路由层 role 白名单：
      1. line_id 必须存在
      2. 对应 submission 的报销单必须归 ctx.user_id
      3. 报销单状态必须是 open 或 needs_revision
      4. 字段必须在 EDITABLE_FIELDS 白名单里

    以上任一不满足返回 {error: ...}。满足则落库 + 审计日志。
    LLM 即便被 prompt injection 诱导传入别人的 line_id，也会被第 2/3 条拦下。
    """
    from backend.db.store import get_submission as _get_sub
    from backend.api.routes.reports import EDITABLE_FIELDS

    line_id = args.get("line_id")
    field = args.get("field")
    value = args.get("value")
    if not line_id or not field:
        return {"error": "line_id 和 field 必填"}
    if field not in EDITABLE_FIELDS:
        return {"error": f"字段 '{field}' 不可编辑", "allowed": sorted(EDITABLE_FIELDS)}

    sub = await _get_sub(db, line_id)
    if not sub:
        return {"error": "行项目不存在"}

    report = await get_report(db, sub.report_id)
    if not report:
        return {"error": "报销单不存在"}
    if report.employee_id != ctx.user_id:
        return {"error": "无权修改（非本人报销单）"}
    if report.status not in ("open", "needs_revision"):
        return {
            "error": f"报销单当前状态 {report.status}，不可编辑。"
                     "提交后需先撤回或等经理退回才能改。"
        }

    # 类型转换
    if field in ("amount", "tax_amount", "exchange_rate"):
        try:
            value = float(value)
        except (ValueError, TypeError):
            return {"error": f"{field} 必须是数字"}

    old_value = getattr(sub, field, None)
    setattr(sub, field, value)
    if field == "exchange_rate" and value is not None:
        sub.converted_amount = round(float(sub.amount) * float(value), 2)
    elif field == "amount" and sub.exchange_rate is not None:
        sub.converted_amount = round(float(value) * float(sub.exchange_rate), 2)
    sub.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(sub)

    await create_audit_log(
        db, actor_id=ctx.user_id, action="line_field_edited",
        resource_type="submission", resource_id=line_id,
        detail={
            "field": field,
            "old": str(old_value),
            "new": str(value),
            "report_id": sub.report_id,
            "via": "chat_employee_drawer",
        },
    )
    return {"ok": True, "line_id": line_id, "field": field, "value": value}


async def tool_check_budget_status(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """查询成本中心预算使用情况及提交指定金额后的预计占比。"""
    from decimal import Decimal as _D
    from backend.db import store as _store

    _cc = (args.get("cost_center") or "").strip()
    _amt = args.get("amount", 0)
    if not _cc:
        return {"error": "缺少 cost_center"}
    try:
        _status = await _store.get_budget_status(db, _cc, _D(str(_amt)))
        if not _status.get("configured"):
            return {"result": f"成本中心 {_cc} 未配置预算。", "configured": False}
        _pct = _status["usage_pct"] * 100
        _proj = _status.get("projected_pct", _status["usage_pct"]) * 100
        _sig = _status["signal"]
        _remaining = _status["total_amount"] - _status["spent_amount"]
        return {
            "result": (
                f"成本中心 {_cc}：当前已用 {_pct:.1f}%，"
                f"本次报销后预计达 {_proj:.1f}%，"
                f"剩余 ¥{_remaining:,.0f}（共 ¥{_status['total_amount']:,.0f}）。"
                f"状态：{_sig}。"
            ),
            "signal": _sig,
            "configured": True,
        }
    except Exception as _e:
        return {"error": f"预算查询失败：{_e}"}


async def tool_get_budget_summary(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """获取当前用户成本中心的预算快照，用于页面加载时主动推送。"""
    from backend.db.store import Employee as _Emp
    from sqlalchemy import select as _sel
    from backend.db import store as _store

    _period = args.get("period") or None
    try:
        _emp_r = await db.execute(_sel(_Emp).where(_Emp.id == ctx.user_id))
        _emp = _emp_r.scalar_one_or_none()
        if not _emp or not _emp.cost_center:
            return {"error": "未找到员工成本中心信息。"}
        _status = await _store.get_budget_status(db, _emp.cost_center, None, _period)
        if not _status.get("configured"):
            return {"result": f"成本中心 {_emp.cost_center} 未配置预算。", "configured": False}
        _pct = _status["usage_pct"] * 100
        _remaining = _status["total_amount"] - _status["spent_amount"]
        _sig = _status["signal"]
        return {
            "result": (
                f"你所在成本中心 {_emp.cost_center} 本季度预算状态："
                f"已用 {_pct:.1f}%（¥{_status['spent_amount']:,.0f} / ¥{_status['total_amount']:,.0f}），"
                f"剩余 ¥{_remaining:,.0f}。状态：{_sig}。"
            ),
            "signal": _sig,
            "configured": True,
            "trend": _status.get("trend"),
        }
    except Exception as _e:
        return {"error": f"预算摘要获取失败：{_e}"}


async def tool_get_policy_rules(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """读取公司报销政策配置，返回结构化规则摘要。"""
    import yaml as _yaml
    _cfg_dir = Path(__file__).resolve().parent.parent.parent.parent / "config"
    try:
        with open(_cfg_dir / "policy.yaml", "r", encoding="utf-8") as f:
            policy = _yaml.safe_load(f)
        with open(_cfg_dir / "expense_types.yaml", "r", encoding="utf-8") as f:
            types = _yaml.safe_load(f)
    except FileNotFoundError:
        return {"error": "政策配置文件未找到"}

    limits = policy.get("limits", {})
    limit_text = []
    for key, tiers in limits.items():
        name = key.replace("_", " ")
        for tier, levels in tiers.items():
            vals = ", ".join(f"{lv}: ¥{v}" if v != "不限" else f"{lv}: 不限" for lv, v in levels.items())
            limit_text.append(f"{name} ({tier}): {vals}")

    expense_cats = []
    for cat_id, cat in types.get("expense_types", {}).items():
        for sub in cat.get("subtypes", []):
            flags = []
            if sub.get("requires_invoice"):
                flags.append("需发票")
            if sub.get("requires_attendee_list"):
                flags.append("需参会人员名单")
            expense_cats.append(f"{cat['name_zh']}/{sub['name_zh']} — {'、'.join(flags) if flags else '无特殊要求'}")

    city_tiers = policy.get("city_tiers", {})
    city_info = []
    for tier, data in city_tiers.items():
        cities = data.get("cities", [])
        city_info.append(f"{tier}: {', '.join(str(c) for c in cities)}")

    payment = policy.get("payment", {})
    tolerance = policy.get("tolerance", {})

    return {
        "company": policy.get("company_info", {}).get("name", ""),
        "employee_levels": [lv["id"] + " " + lv["name"] for lv in policy.get("employee_levels", [])],
        "city_tiers": city_info,
        "limits": limit_text,
        "expense_categories": expense_cats,
        "payment_rules": {
            "bank_transfer_threshold": f"≥¥{payment.get('bank_transfer_threshold', 5000)} 走银行转账",
            "petty_cash_max": f"<¥{payment.get('petty_cash_max', 5000)} 可走备用金",
        },
        "tolerance_rules": {
            "warning_threshold": f"超标 ≤¥{tolerance.get('warning_threshold', 50)} 为警告（可通过）",
            "reject_above": f"超标 >¥{tolerance.get('reject_above', 50)} 为拒绝",
        },
    }


async def tool_get_pending_approval_queue(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：当前角色对应的待审 Report 列表 + 行级风险概要。

    manager → status=pending（员工刚提交）
    finance_admin → status=manager_approved（经理批了，等财务）

    仅对 manager / finance_admin 角色开放；其他角色拿到 error。
    """
    if ctx.role not in ("manager", "finance_admin"):
        return {"error": "权限不足：仅经理/财务可查看待审队列"}

    target_status = "pending" if ctx.role == "manager" else "manager_approved"
    limit = int(args.get("limit") or 20)
    min_risk = args.get("min_risk_score")
    try:
        min_risk_val = float(min_risk) if min_risk is not None else None
    except (TypeError, ValueError):
        min_risk_val = None

    from backend.db.store import Report, list_report_submissions
    from sqlalchemy import select as _select
    result = await db.execute(
        _select(Report)
        .where(Report.status == target_status)
        .order_by(Report.submitted_at.asc().nullsfirst())
    )
    reports = list(result.scalars().all())

    items = []
    today = date.today()
    for r in reports:
        subs = await list_report_submissions(db, r.id)
        if not subs:
            continue
        emp = await get_employee(db, r.employee_id)
        max_risk = max(
            (float(s.risk_score) for s in subs if s.risk_score is not None),
            default=0.0,
        )
        if min_risk_val is not None and max_risk < min_risk_val:
            continue
        tier_order = {"T4": 4, "T3": 3, "T2": 2, "T1": 1}
        worst_tier = None
        for s in subs:
            if s.tier and tier_order.get(s.tier, 0) > tier_order.get(worst_tier, 0):
                worst_tier = s.tier
        total_amount = sum(float(s.amount) for s in subs)
        days_waiting = None
        if r.submitted_at:
            try:
                days_waiting = (today - r.submitted_at.date()).days
            except Exception:
                days_waiting = None
        items.append({
            "report_id": r.id,
            "title": r.title,
            "employee_id": r.employee_id,
            "employee_name": emp.name if emp else r.employee_id,
            "department": emp.department if emp else None,
            "line_count": len(subs),
            "total_amount": round(total_amount, 2),
            "max_risk_score": round(max_risk, 1),
            "worst_tier": worst_tier,
            "days_waiting": days_waiting,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
        })

    items.sort(key=lambda x: (-(x["max_risk_score"] or 0), -(x["days_waiting"] or 0)))
    items = items[:limit]
    return {
        "role": ctx.role,
        "queue_status": target_status,
        "total_count": len(items),
        "items": items,
    }


async def tool_get_team_spend_summary(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """只读：某 department 在 period 内的报销聚合（CNY），by category + top employees。

    manager → 强制本部门（无视 args.department）
    finance_admin → 任意 department；不传则全公司

    period: month=当前自然月，quarter=当前自然季度
    """
    if ctx.role not in ("manager", "finance_admin"):
        return {"error": "权限不足：仅经理/财务可查看团队总览"}

    period = args.get("period")
    if period not in ("month", "quarter"):
        return {"error": "period 必须是 'month' 或 'quarter'"}

    today = date.today()
    if period == "month":
        start = today.replace(day=1)
        label = f"{today.year}-{today.month:02d}"
    else:
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=q_start_month, day=1)
        label = f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    start_iso = start.isoformat()

    requested_dept = (args.get("department") or "").strip() or None
    category_filter = (args.get("category") or "").strip() or None

    if ctx.role == "manager":
        viewer = await get_employee(db, ctx.user_id)
        scoped_dept = viewer.department if viewer else None
        if scoped_dept is None:
            return {"error": "无法确定 manager 所属部门，请联系管理员"}
    else:
        scoped_dept = requested_dept

    from backend.services.fx_service import convert as fx_convert
    page = await list_submissions(db, page=1, page_size=2000)
    home_cur = "CNY"

    total_home = 0.0
    count = 0
    by_cat: dict[str, dict] = {}
    by_emp: dict[str, dict] = {}

    for s in page["items"]:
        if (s.date or "") < start_iso and not (s.created_at and s.created_at.date() >= start):
            continue
        if scoped_dept is not None and (s.department or None) != scoped_dept:
            continue
        if category_filter and (s.category or "") != category_filter:
            continue
        orig_amt = float(s.amount)
        currency = s.currency or home_cur
        if s.exchange_rate is not None:
            home_amt = round(orig_amt * float(s.exchange_rate), 2)
        elif currency != home_cur:
            home_amt = fx_convert(orig_amt, currency, home_cur)
        else:
            home_amt = orig_amt
        total_home += home_amt
        count += 1
        cat = s.category or "other"
        bucket = by_cat.setdefault(cat, {"category": cat, "amount_home": 0.0, "count": 0})
        bucket["amount_home"] += home_amt
        bucket["count"] += 1
        emp_bucket = by_emp.setdefault(s.employee_id, {
            "employee_id": s.employee_id, "amount_home": 0.0, "count": 0,
        })
        emp_bucket["amount_home"] += home_amt
        emp_bucket["count"] += 1

    top_emps = sorted(by_emp.values(), key=lambda x: x["amount_home"], reverse=True)[:5]
    for e in top_emps:
        emp = await get_employee(db, e["employee_id"])
        e["employee_name"] = emp.name if emp else e["employee_id"]
        e["amount_home"] = round(e["amount_home"], 2)

    return {
        "period": period,
        "period_label": label,
        "department": scoped_dept,
        "department_scope": "self" if ctx.role == "manager" else ("all" if scoped_dept is None else "specified"),
        "category_filter": category_filter,
        "home_currency": home_cur,
        "total_home": round(total_home, 2),
        "count": count,
        "by_category": sorted(
            [{**b, "amount_home": round(b["amount_home"], 2)} for b in by_cat.values()],
            key=lambda x: x["amount_home"], reverse=True,
        ),
        "top_employees": top_emps,
    }


TOOL_HANDLERS = {
    "extract_receipt_fields":            tool_extract_receipt_fields,
    "suggest_category":                  tool_suggest_category,
    "check_duplicate_invoice":           tool_check_duplicate_invoice,
    "get_my_recent_submissions":         tool_get_my_recent_submissions,
    "get_report_detail":                 tool_get_report_detail,
    "get_spend_summary":                 tool_get_spend_summary,
    "get_submission_for_review":         tool_get_submission_for_review,
    "get_employee_submission_history":   tool_get_employee_submission_history,
    "update_draft_field":                tool_update_draft_field,
    "update_report_line_field":          tool_update_report_line_field,
    "check_budget_status":               tool_check_budget_status,
    "get_budget_summary":                tool_get_budget_summary,
    "get_policy_rules":                  tool_get_policy_rules,
    "get_pending_approval_queue":        tool_get_pending_approval_queue,
    "get_team_spend_summary":            tool_get_team_spend_summary,
}


# ═══════════════════════════════════════════════════════════════════
# LLM 抽象层 — MockLLM 规则脚本 / RealLLM 预留
# ═══════════════════════════════════════════════════════════════════

class LLMResponse:
    """LLM 一轮响应的抽象 — 对应 Anthropic API 的 Message 结构。"""
    def __init__(
        self,
        text: str = "",
        tool_calls: Optional[list[dict]] = None,
        stop_reason: str = "end_turn",
    ):
        self.text = text
        self.tool_calls = tool_calls or []  # [{id, name, input}, ...]
        self.stop_reason = stop_reason      # "end_turn" | "tool_use"


class BaseLLM:
    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "employee_submit",
    ) -> LLMResponse:
        raise NotImplementedError


class MockLLM(BaseLLM):
    """规则脚本化的 "LLM"，用来在没有 API Key 时 demo agent 架构。

    决策逻辑：看消息历史的最新一条，决定下一步。
    不做任何 LLM 推理——纯状态机。
    """

    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "employee_submit",
    ) -> LLMResponse:
        # "employee" role = unified drawer. Route it through the
        # conservative QA script (no OCR/write side effects unless the
        # user explicitly asks). Real LLM is recommended for this role.
        if agent_role == "employee":
            return self._qa_turn(messages)
        if agent_role == "manager":
            return self._manager_turn(messages)
        # 默认：employee_submit 脚本
        # 扫描历史找：最后一条 user 消息、是否有 extract 结果、是否有 dup 结果、是否已有 suggest 结果
        last_user_idx = self._find_last(messages, role="user", text_not_tool=True)
        last_user_text = ""
        if last_user_idx is not None:
            last_user_text = self._extract_text(messages[last_user_idx]).lower()

        extract_result = self._find_tool_result(messages, "extract_receipt_fields")
        dup_result     = self._find_tool_result(messages, "check_duplicate_invoice")
        suggest_result = self._find_tool_result(messages, "suggest_category")

        # 第 1 步：还没 extract 过 → 只要有用户消息就自动触发识别
        # 真实 LLM 会根据上下文判断；MockLLM 用简单规则：没 extract 结果就先做这一步
        if extract_result is None and last_user_idx is not None:
            return LLMResponse(
                text="好，我先识别一下您上传的发票图片…",
                tool_calls=[self._tool_call("extract_receipt_fields", {})],
                stop_reason="tool_use",
            )

        # 第 2 步：有 extract 结果但还没查重 → check_duplicate
        if extract_result and not extract_result.get("error") and dup_result is None:
            inv = extract_result.get("invoice_number")
            if inv:
                return LLMResponse(
                    text=f"识别成功！商户：**{extract_result.get('merchant', '—')}**，金额：¥{extract_result.get('amount', 0)}。我先查一下发票号是否重复…",
                    tool_calls=[self._tool_call("check_duplicate_invoice", {"invoice_number": inv})],
                    stop_reason="tool_use",
                )

        # 第 2b 步：查重发现重复 → 直接结束
        if dup_result and dup_result.get("is_duplicate"):
            existing = dup_result.get("existing_submission_id", "")
            return LLMResponse(
                text=f"⚠️ 这张发票已被报销过（单据 #{existing[:8]}），不能重复提交。请检查是否上传了正确的发票。",
                stop_reason="end_turn",
            )

        # 第 3 步：有 extract 且不重复，但还没推荐类别 → suggest
        if extract_result and not extract_result.get("error") and suggest_result is None:
            merchant = extract_result.get("merchant", "")
            return LLMResponse(
                text="发票号 OK，没有重复。让我根据商户名称推荐一个类别…",
                tool_calls=[self._tool_call("suggest_category", {"merchant": merchant})],
                stop_reason="tool_use",
            )

        # 第 4 步：所有查询都完成 → 批量写入 draft
        if extract_result and suggest_result and not self._has_draft_writes(messages):
            tc = []
            f = extract_result
            tc.append(self._tool_call("update_draft_field", {
                "field": "merchant", "value": str(f.get("merchant") or ""), "source": "ocr"}))
            tc.append(self._tool_call("update_draft_field", {
                "field": "amount", "value": str(f.get("amount") or 0), "source": "ocr"}))
            tc.append(self._tool_call("update_draft_field", {
                "field": "date", "value": str(f.get("date") or ""), "source": "ocr"}))
            tc.append(self._tool_call("update_draft_field", {
                "field": "tax_amount", "value": str(f.get("tax_amount") or 0), "source": "ocr"}))
            if f.get("invoice_number"):
                tc.append(self._tool_call("update_draft_field", {
                    "field": "invoice_number", "value": f["invoice_number"], "source": "ocr"}))
            if f.get("invoice_code"):
                tc.append(self._tool_call("update_draft_field", {
                    "field": "invoice_code", "value": f["invoice_code"], "source": "ocr"}))
            tc.append(self._tool_call("update_draft_field", {
                "field": "category", "value": suggest_result.get("category", "other"),
                "source": "agent_suggested"}))
            if f.get("description"):
                tc.append(self._tool_call("update_draft_field", {
                    "field": "description", "value": f["description"], "source": "ocr"}))
            return LLMResponse(
                text=f"推荐类别：**{self._cat_label(suggest_result.get('category'))}**（置信度 {int(suggest_result.get('confidence', 0) * 100)}%）。我把所有字段填到左侧表单了，请您检查——如需修改某个字段，告诉我即可。",
                tool_calls=tc,
                stop_reason="tool_use",
            )

        # 第 5 步：已写入 draft → 结束，提示用户确认
        if self._has_draft_writes(messages):
            return LLMResponse(
                text="✅ 所有字段已填入左侧表单。您可以：\n\n• 检查确认后点击「提交报销单」\n• 告诉我需要修改什么（例如：把金额改成 500）\n• 换一个类别（例如：这是团建不是餐饮）",
                stop_reason="end_turn",
            )

        # 用户后续说"改 XX"
        if "改" in last_user_text or "换" in last_user_text or "修改" in last_user_text:
            return LLMResponse(
                text="明白，请告诉我具体要改哪个字段改成什么值。例如：'把金额改成 380' 或 '类别改成 entertainment'。",
                stop_reason="end_turn",
            )

        # 默认欢迎语
        return LLMResponse(
            text="你好！我是报销助手。您可以：\n\n• 上传发票图，我帮您自动识别字段\n• 直接告诉我您要报销什么\n• 让我查一下您的历史报销记录\n\n请开始吧～",
            stop_reason="end_turn",
        )

    # ── employee drawer (read-mostly) 分支 ──
    def _qa_turn(self, messages: list[dict]) -> LLMResponse:
        """只读 QA 模式的规则脚本（MockLLM 下 employee role 走这条）。

        两轮循环：第 1 轮根据最新 user 文本决定调哪个 tool；第 2 轮看到
        tool 结果 → 格式化成自然语言 → end_turn。纯关键词匹配，零推理。
        """
        # 先看是否已有本轮的 tool 结果 —— 有就直接产出最终文本
        summary = self._find_tool_result(messages, "get_spend_summary")
        detail  = self._find_tool_result(messages, "get_report_detail")
        recent  = self._find_tool_result(messages, "get_my_recent_submissions")
        policy  = self._find_tool_result(messages, "get_policy_rules")

        if summary is not None:
            return LLMResponse(text=self._fmt_summary(summary), stop_reason="end_turn")
        if detail is not None:
            return LLMResponse(text=self._fmt_detail(detail), stop_reason="end_turn")
        if recent is not None:
            return LLMResponse(text=self._fmt_recent(recent), stop_reason="end_turn")
        if policy is not None:
            return LLMResponse(text=self._fmt_policy(policy), stop_reason="end_turn")

        # 否则根据最新 user 文本决定要调哪个 tool
        last_idx = self._find_last(messages, role="user", text_not_tool=True)
        text = self._extract_text(messages[last_idx]).lower() if last_idx is not None else ""

        spend_kws  = ("花", "总共", "消费", "多少", "spend", "summary", "汇总")
        detail_kws = ("详情", "状态", "那笔", "这笔", "上笔", "上一笔", "上次")
        recent_kws = ("最近", "历史", "recent", "list", "有哪些")
        policy_kws = ("政策", "规定", "限额", "标准", "policy", "报销政策", "能报", "可以报", "允许")

        if any(k in text for k in policy_kws):
            return LLMResponse(
                text="我帮你查一下公司报销政策…",
                tool_calls=[self._tool_call("get_policy_rules", {})],
                stop_reason="tool_use",
            )
        if any(k in text for k in spend_kws):
            period = "quarter" if any(k in text for k in ("季度", "quarter", "本季", "这季")) else "month"
            return LLMResponse(
                text=f"好的，我查一下你{'本季度' if period == 'quarter' else '本月'}的消费汇总…",
                tool_calls=[self._tool_call("get_spend_summary", {"period": period})],
                stop_reason="tool_use",
            )
        if any(k in text for k in detail_kws) or any(k in text for k in recent_kws):
            return LLMResponse(
                text="我先拉一下你最近的报销记录…",
                tool_calls=[self._tool_call("get_my_recent_submissions", {})],
                stop_reason="tool_use",
            )

        return LLMResponse(
            text=(
                "你好！我是「我的报销」助手。你可以问我：\n\n"
                "• 我这个月花了多少？\n"
                "• 本季度的消费汇总是多少？\n"
                "• 最近有哪些报销记录？\n"
                "• 上一笔报销是什么状态？\n"
                "• 报销政策是什么？限额多少？"
            ),
            stop_reason="end_turn",
        )

    def _manager_turn(self, messages: list[dict]) -> LLMResponse:
        """经理 / 财务的 drawer chat 规则脚本。

        三类高频意图：
        1. "为什么风险高 / why" + 当前报销单上下文 → get_submission_for_review
        2. "待审 / 队列 / 等我批" → get_pending_approval_queue
        3. "团队本月花 / 部门" → get_team_spend_summary
        """
        review = self._find_tool_result(messages, "get_submission_for_review")
        queue  = self._find_tool_result(messages, "get_pending_approval_queue")
        team   = self._find_tool_result(messages, "get_team_spend_summary")

        if review is not None:
            return LLMResponse(text=self._fmt_review(review), stop_reason="end_turn")
        if queue is not None:
            return LLMResponse(text=self._fmt_queue(queue), stop_reason="end_turn")
        if team is not None:
            return LLMResponse(text=self._fmt_team(team), stop_reason="end_turn")

        last_idx = self._find_last(messages, role="user", text_not_tool=True)
        text = self._extract_text(messages[last_idx]).lower() if last_idx is not None else ""

        # Pull the highest-risk line_id out of the injected page context, if any.
        ctx_line_id = None
        ctx_text = ""
        for m in messages:
            content = self._extract_text(m)
            if "[当前上下文]" in content:
                ctx_text = content
                break
        if ctx_text:
            best_risk = -1.0
            for ln in ctx_text.splitlines():
                if "line#" not in ln or "id=" not in ln:
                    continue
                try:
                    sid = ln.split("id=", 1)[1].split(" |", 1)[0].strip()
                except IndexError:
                    continue
                risk_val = -1.0
                if "risk=" in ln:
                    try:
                        risk_val = float(ln.split("risk=", 1)[1].split(" ", 1)[0])
                    except ValueError:
                        risk_val = -1.0
                if risk_val > best_risk:
                    best_risk = risk_val
                    ctx_line_id = sid

        why_kws    = ("为什么", "why", "原因", "怎么", "高风险", "风险", "解释")
        queue_kws  = ("待审", "队列", "等我", "等审", "需要我", "queue", "pending", "approval")
        team_kws   = ("团队", "部门", "team", "本月花", "本季度", "department", "支出")

        if any(k in text for k in why_kws) and ctx_line_id:
            return LLMResponse(
                text="我去拉一下这张单的审计报告…",
                tool_calls=[self._tool_call(
                    "get_submission_for_review", {"submission_id": ctx_line_id},
                )],
                stop_reason="tool_use",
            )
        if any(k in text for k in queue_kws):
            min_risk = 80.0 if any(k in text for k in ("高风险", "high risk", "高的")) else None
            args = {"limit": 20}
            if min_risk:
                args["min_risk_score"] = min_risk
            return LLMResponse(
                text="我看一下你的待审队列…",
                tool_calls=[self._tool_call("get_pending_approval_queue", args)],
                stop_reason="tool_use",
            )
        if any(k in text for k in team_kws):
            period = "quarter" if any(k in text for k in ("季度", "quarter", "本季", "这季")) else "month"
            return LLMResponse(
                text=f"我聚合一下你团队{'本季度' if period == 'quarter' else '本月'}的报销…",
                tool_calls=[self._tool_call("get_team_spend_summary", {"period": period})],
                stop_reason="tool_use",
            )

        return LLMResponse(
            text=(
                "你好！我是经理审批助手。你可以问我：\n\n"
                "• 这张单为什么风险这么高？\n"
                "• 我现在有哪些待审报销？\n"
                "• 只看高风险（>=80）的待审报销\n"
                "• 我团队本月花了多少？\n"
                "• 报销政策是什么？"
            ),
            stop_reason="end_turn",
        )

    @staticmethod
    def _fmt_review(r: dict) -> str:
        if r.get("error"):
            return f"读取失败：{r['error']}"
        risk = r.get("risk_score")
        tier = r.get("tier") or "-"
        merchant = r.get("merchant") or "-"
        amount = r.get("amount")
        currency = r.get("currency") or ""
        audit = r.get("audit_report") or {}
        signals = audit.get("fraud_signals") or []
        investigation = audit.get("investigation") or {}

        lines = [
            f"🔍 **{merchant}** · {currency} {amount} · 风险 **{risk}/100** ({tier})",
            "",
        ]
        if signals:
            lines.append("**触发的规则：**")
            for s in signals[:5]:
                rid = s.get("rule_id") or s.get("rule") or "?"
                score = s.get("score", 0)
                ev = s.get("evidence") or ""
                lines.append(f"• `{rid}` (+{score})  {ev}")
            lines.append("")
        if investigation:
            verdict = investigation.get("verdict", "-")
            confidence = investigation.get("confidence", 0)
            summary = investigation.get("summary") or ""
            lines.append(f"**OODA 调查结论：** {verdict}（置信度 {confidence:.0%}）")
            if summary:
                lines.append(f"> {summary}")
        if not signals and not investigation:
            lines.append("（这笔没有触发任何欺诈规则，也没有触发 OODA 调查。风险分主要来自 ambiguity 维度。）")
        return "\n".join(lines)

    @staticmethod
    def _fmt_queue(q: dict) -> str:
        if q.get("error"):
            return f"查询失败：{q['error']}"
        items = q.get("items") or []
        scope = q.get("queue_status", "?")
        if not items:
            return f"📋 当前没有 status={scope} 的待审报销。"
        lines = [f"📋 **{len(items)} 张待审报销**（{scope}）"]
        for it in items[:10]:
            risk = it.get("max_risk_score", 0) or 0
            wait = it.get("days_waiting")
            wait_str = f"已等 {wait}d" if wait is not None else ""
            tier = it.get("worst_tier") or ""
            lines.append(
                f"• {it.get('employee_name', '-')} · ¥{it.get('total_amount', 0):,.0f} · "
                f"{it.get('line_count', 0)} 笔 · risk={risk:.0f} {tier} · {wait_str}"
            )
        return "\n".join(lines)

    @staticmethod
    def _fmt_team(t: dict) -> str:
        if t.get("error"):
            return f"查询失败：{t['error']}"
        label = t.get("period_label", "?")
        dept = t.get("department") or "全公司"
        total = t.get("total_home", 0)
        count = t.get("count", 0)
        cur = t.get("home_currency", "CNY")
        lines = [f"📊 **{dept} · {label}**：{count} 笔，合计 **{cur} {total:,.2f}**"]
        cats = t.get("by_category") or []
        if cats:
            lines.append("")
            lines.append("**按类别：**")
            for c in cats[:5]:
                lines.append(f"• {c['category']}: {cur} {c['amount_home']:,.2f} ({c['count']} 笔)")
        emps = t.get("top_employees") or []
        if emps:
            lines.append("")
            lines.append("**Top 5 员工：**")
            for e in emps:
                lines.append(f"• {e.get('employee_name', '-')}: {cur} {e['amount_home']:,.2f} ({e['count']} 笔)")
        return "\n".join(lines)

    @staticmethod
    def _fmt_summary(s: dict) -> str:
        if s.get("error"):
            return f"查询失败：{s['error']}"
        label = s.get("period_label") or s.get("period") or ""
        home_cur = s.get("home_currency", "CNY")
        total_home = s.get("total_home", s.get("total_cny", s.get("total", 0)))
        count = s.get("count", 0)
        lines = [f"📊 **{label}** 消费汇总：共 {count} 笔，合计 **≈ {home_cur} {total_home:,.2f}**"]
        items = s.get("items") or []
        if items:
            lines.append("")
            lines.append("明细：")
            for it in items:
                cur = it.get("currency", home_cur)
                amt = it.get("amount", 0)
                home_amt = it.get("amount_home", it.get("amount_cny", amt))
                merchant = it.get("merchant", "")
                dt = it.get("date", "")
                if cur != home_cur:
                    lines.append(f"• {dt} {merchant}：{cur} {amt:,.2f}（≈ {home_cur} {home_amt:,.2f}）")
                else:
                    lines.append(f"• {dt} {merchant}：{home_cur} {amt:,.2f}")
        by_cat = s.get("by_category") or []
        if by_cat:
            lines.append("")
            lines.append("按类别：")
            cat_label = {"meal": "餐饮", "transport": "交通", "accommodation": "住宿",
                         "entertainment": "招待", "other": "其他"}
            for b in by_cat:
                amt_h = b.get("amount_home", b.get("amount_cny", b.get("amount", 0)))
                lines.append(f"• {cat_label.get(b['category'], b['category'])}：≈ {home_cur} {amt_h:,.2f}（{b['count']} 笔）")
        elif count == 0:
            lines.append("\n本期还没有报销记录。")
        return "\n".join(lines)

    @staticmethod
    def _fmt_detail(d: dict) -> str:
        if d.get("error"):
            return f"查询失败：{d['error']}"
        status_label = {
            "processing": "AI 审核中", "reviewed": "AI 审核通过",
            "manager_approved": "经理已批准", "finance_approved": "财务已批准",
            "exported": "已导出", "rejected": "已驳回", "review_failed": "AI 审核未通过",
        }.get(d.get("status") or "", d.get("status") or "—")
        lines = [
            f"📄 单据 #{(d.get('id') or '')[:8]} — **{status_label}**",
            f"• 商户：{d.get('merchant', '—')}",
            f"• 金额：¥{d.get('amount', 0):,.2f} {d.get('currency') or ''}",
            f"• 类别：{d.get('category', '—')}",
            f"• 日期：{d.get('date', '—')}",
        ]
        if d.get("approver_comment"):
            lines.append(f"• 审批备注：{d['approver_comment']}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_recent(r: dict) -> str:
        items = r.get("items") or []
        if not items:
            return "你最近没有报销记录。"
        lines = [f"📋 最近 {len(items)} 笔报销："]
        for i, it in enumerate(items, 1):
            lines.append(
                f"{i}. {it.get('merchant', '—')} · ¥{it.get('amount', 0):,.2f} · "
                f"{it.get('category', '—')} · {it.get('date', '—')} · {it.get('status', '—')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _fmt_policy(p: dict) -> str:
        if p.get("error"):
            return f"查询失败：{p['error']}"
        lines = [f"📋 **{p.get('company', '')}** 报销政策"]
        lines.append("")
        lines.append("**费用类别及要求：**")
        for cat in p.get("expense_categories", []):
            lines.append(f"• {cat}")
        lines.append("")
        lines.append("**限额标准（按城市等级×员工等级）：**")
        for lim in p.get("limits", []):
            lines.append(f"• {lim}")
        lines.append("")
        lines.append("**付款规则：**")
        pr = p.get("payment_rules", {})
        for v in pr.values():
            lines.append(f"• {v}")
        lines.append("")
        lines.append("**超标处理：**")
        tr = p.get("tolerance_rules", {})
        for v in tr.values():
            lines.append(f"• {v}")
        return "\n".join(lines)

    # ── helpers ──

    @staticmethod
    def _tool_call(name: str, inp: dict) -> dict:
        return {"id": f"tool_{uuid.uuid4().hex[:12]}", "name": name, "input": inp}

    @staticmethod
    def _extract_text(msg: dict) -> str:
        c = msg.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else ""
                for b in c
            )
        return ""

    @staticmethod
    def _find_last(messages: list[dict], role: str, text_not_tool: bool = False) -> Optional[int]:
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") != role:
                continue
            if text_not_tool:
                c = m.get("content")
                if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c
                ):
                    continue
            return i
        return None

    @staticmethod
    def _find_tool_result(messages: list[dict], tool_name: str) -> Optional[dict]:
        """从消息历史里找某个 tool 的最新结果。"""
        # 先找 tool_use.id → 再找同 id 的 tool_result
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") != "assistant":
                continue
            content = m.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == tool_name:
                    tuid = block.get("id")
                    # 在后续消息里找对应的 tool_result
                    for j in range(i + 1, len(messages)):
                        mj = messages[j]
                        if mj.get("role") != "user":
                            continue
                        cj = mj.get("content", [])
                        if not isinstance(cj, list):
                            continue
                        for bj in cj:
                            if isinstance(bj, dict) and bj.get("type") == "tool_result" and bj.get("tool_use_id") == tuid:
                                raw = bj.get("content", "")
                                if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                                    raw = raw[0].get("text", "")
                                try:
                                    return json.loads(raw) if isinstance(raw, str) else raw
                                except json.JSONDecodeError:
                                    return {"_raw": raw}
        return None

    def _has_draft_writes(self, messages: list[dict]) -> bool:
        for m in messages:
            if m.get("role") != "assistant":
                continue
            content = m.get("content", [])
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "update_draft_field":
                    return True
        return False

    @staticmethod
    def _cat_label(cat: str) -> str:
        return {"meal": "餐饮", "transport": "交通", "accommodation": "住宿",
                "entertainment": "招待", "other": "其他"}.get(cat, cat or "其他")


_SYSTEM_PROMPTS: dict[str, str] = {
    "employee_submit": (
        "你是企业报销助手，帮助员工填写报销单草稿（draft）。请用中文回复，简洁专业。\n\n"
        "可用工具：识别发票图片、推荐费用类别、检查重复发票、查历史报销记录、更新草稿字段。\n"
        "重要约束：你只能修改草稿（draft），不能提交或审批报销单。提交必须由员工手动确认。"
        "\n\n字段修改规则：当用户要求修改某个字段（例如'把金额改成 380'、'类型改成餐饮'、'费用类型改成餐饮'），"
        "你必须立即调用 update_draft_field 工具执行修改，不要只回复建议文字。"
        "类别名称映射：餐饮=meal、交通=transport、住宿=accommodation、招待/团建=entertainment、其他=other。"
        "可修改的字段：merchant、amount、category、date、tax_amount、invoice_number、invoice_code、project_code、description、currency。"
        "\n\n预算检查规则：员工填写金额后，调用 check_budget_status（使用员工的成本中心和填写金额）。如果 signal 为 'info'，告知预算使用情况和预计占比。如果 signal 为 'blocked' 或 'over_budget'，明确告知提交后将被财务管理员拦截审核，不要隐瞒。如果 signal 为 'ok' 或成本中心未配置预算，无需提及预算。"
    ),
    "employee": (
        "你是企业报销助手。请镜像用户语言（中/英），简洁作答。\n\n"
        "可做：查报销记录、查预算、查政策、改自己 open/needs_revision 状态报销单里的行字段。\n"
        "不可做：提交报销单、审批、拒绝、付款——这类动作必须用户在 UI 点按钮完成，"
        "即使用户要求你执行，也要婉拒并指引到对应按钮。\n\n"
        "字段修改规则：用户要改字段时调用 update_report_line_field，工具会自己检查归属和状态。\n"
        "类别映射：餐饮=meal、交通=transport、住宿=accommodation、招待/团建=entertainment、其他=other。\n"
        "可改字段：merchant、amount、category、date、tax_amount、invoice_number、invoice_code、project_code、description、currency。\n\n"
        "报销单页加载规则（trigger=page_load + page=my-reports）：调一次 get_budget_summary；"
        "signal=info/blocked/over_budget → 一行预算提示；signal=ok 或工具返回 error → 保持静默。\n\n"
        "简短确认（OK/好的/嗯/thanks）不视为新请求，简单回应或静默即可。\n"
        "闲聊/无关问题一句话拒答并提示可问什么。"
    ),
    "manager_explain": (
        "你是审批辅助助手，帮助经理理解报销单的风险情况。"
        "你只能读取报销数据，不能修改任何内容。请用中文回复，提供简洁的风险摘要。"
    ),
}


class RealLLM(BaseLLM):
    """GPT-4o 真实 API 调用。设置 OPENAI_API_KEY + AGENT_USE_REAL_LLM=1 启用。

    消息格式转换：内部使用 Anthropic 格式（tool_use / tool_result blocks），
    发送给 OpenAI 前翻译成 OpenAI format（tool_calls / role=tool），
    返回后再翻译回 LLMResponse。外层 run_agent loop 无需任何修改。
    """

    def __init__(self) -> None:
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            self._model = os.getenv("OPENAI_MODEL", "gpt-4o")
        except Exception as exc:
            raise RuntimeError(f"OpenAI SDK 初始化失败：{exc}") from exc

    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "employee_submit",
    ) -> LLMResponse:
        from backend.services.trace import record_trace, TraceTimer

        # Prefer the dashboard-edited prompt (eval_prompts.json), fall back to
        # the hardcoded default if the JSON entry is missing or empty. Loaded
        # per-request so dashboard edits flow in without a server restart.
        system = (
            load_prompt(f"chat_{agent_role}")
            or _SYSTEM_PROMPTS.get(agent_role, _SYSTEM_PROMPTS["employee_submit"])
        )
        oai_messages = self._to_oai_messages(messages, system)
        oai_tools = self._to_oai_tools(tools)

        kwargs: dict = {
            "model": self._model,
            "messages": oai_messages,
            "max_tokens": 2048,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
            kwargs["tool_choice"] = "auto"

        text = ""
        tool_calls: list[dict] = []
        stop_reason = "end_turn"
        usage: Optional[dict] = None
        err: Optional[str] = None
        timer = TraceTimer()
        try:
            with timer:
                response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message
            text = msg.content or ""
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        inp = json.loads(tc.function.arguments)
                    except (json.JSONDecodeError, ValueError):
                        inp = {}
                    tool_calls.append({"id": tc.id, "name": tc.function.name, "input": inp})
            stop_reason = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"
            if getattr(response, "usage", None):
                usage = {"input": response.usage.prompt_tokens, "output": response.usage.completion_tokens}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await record_trace(
                component=f"chat_{agent_role}",
                model=self._model,
                prompt=oai_messages,
                response=text or None,
                parsed_output={"tool_calls": tool_calls, "stop_reason": stop_reason} if not err else None,
                latency_ms=timer.elapsed_ms or None,
                token_usage=usage,
                error=err,
            )

        return LLMResponse(text=text, tool_calls=tool_calls, stop_reason=stop_reason)

    # ── Format translators ─────────────────────────────────────────

    @staticmethod
    def _to_oai_messages(messages: list[dict], system: str) -> list[dict]:
        """Anthropic 内部消息格式 → OpenAI API 格式。"""
        result: list[dict] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                continue

            if role == "assistant":
                texts = [
                    b["text"] for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                oai_tool_calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                        },
                    }
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                oai_msg: dict = {
                    "role": "assistant",
                    "content": " ".join(texts) if texts else None,
                }
                if oai_tool_calls:
                    oai_msg["tool_calls"] = oai_tool_calls
                result.append(oai_msg)

            elif role == "user":
                tool_results = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                text_blocks = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                # tool results → role=tool messages (must follow the assistant turn)
                for tr in tool_results:
                    raw = tr.get("content", "")
                    if isinstance(raw, list) and raw:
                        raw = raw[0].get("text", "") if isinstance(raw[0], dict) else str(raw[0])
                    result.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": raw if isinstance(raw, str) else json.dumps(raw),
                    })
                if text_blocks:
                    joined = " ".join(b.get("text", "") for b in text_blocks)
                    result.append({"role": "user", "content": joined})

        return result

    @staticmethod
    def _to_oai_tools(tools: list[dict]) -> list[dict]:
        """Anthropic tool schema → OpenAI function schema。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]


def get_llm() -> BaseLLM:
    """根据环境切换 LLM backend。

    MockLLM（默认）：无需任何 API Key，规则脚本，支持全套 eval。
    RealLLM（GPT-4o）：设置 OPENAI_API_KEY + AGENT_USE_REAL_LLM=1。
    """
    if os.getenv("OPENAI_API_KEY") and os.getenv("AGENT_USE_REAL_LLM") == "1":
        return RealLLM()
    return MockLLM()


# ═══════════════════════════════════════════════════════════════════
# Agent Loop — 真实架构，只是 LLM 是 Mock
# ═══════════════════════════════════════════════════════════════════

async def run_agent(
    user_message: str,
    draft_id: Optional[str],
    ctx: UserContext,
    db: AsyncSession,
    agent_role: str = "employee_submit",
    messages_history: Optional[list[dict]] = None,
    extra_handlers: Optional[dict] = None,
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
    llm = get_llm()
    allowed_tool_names = set(TOOL_REGISTRY.get(agent_role, []))
    tools_for_llm = get_tools_for_role(agent_role)

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
        messages = list(draft.chat_history or [])
        new_user_msg = {"role": "user", "content": user_message}
        messages.append(new_user_msg)
        new_messages_to_persist = [new_user_msg]
    else:
        # Stateless QA 模式——调用方全权管理历史，后端不持久化
        messages = list(messages_history or [])
        new_messages_to_persist = None

    yield {"type": "message_start"}

    # Agent loop — 最多 10 轮防爆
    for _ in range(10):
        response = await llm.next_turn(messages, tools_for_llm, agent_role=agent_role)

        if response.text:
            yield {"type": "assistant_text", "text": response.text}

        # 构造 assistant turn（包含 text + tool_use blocks）
        assistant_content: list[dict] = []
        if response.text:
            assistant_content.append({"type": "text", "text": response.text})
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

        if response.stop_reason == "end_turn":
            yield {"type": "message_end", "stop_reason": "end_turn"}
            break

        # 执行工具
        if response.stop_reason == "tool_use" and response.tool_calls:
            tool_results_content: list[dict] = []
            draft_changed = False
            for tc in response.tool_calls:
                yield {"type": "tool_call", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                # 白名单强制——防 prompt injection 的最后一道闸
                if tc["name"] not in allowed_tool_names:
                    result = {
                        "error": f"tool '{tc['name']}' not allowed for role '{agent_role}'",
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
                yield {"type": "tool_result", "id": tc["id"], "name": tc["name"], "result": result}
                if tc["name"] == "update_draft_field" and result.get("ok"):
                    draft_changed = True
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            if draft_changed and draft_id is not None:
                # 推一条"draft 已更新"事件给前端，让左侧表单实时同步
                fresh = await get_draft(db, draft_id)
                yield {
                    "type": "draft_updated",
                    "fields": fresh.fields or {},
                    "field_sources": fresh.field_sources or {},
                }

            tool_user_msg = {"role": "user", "content": tool_results_content}
            messages.append(tool_user_msg)
            if new_messages_to_persist is not None:
                new_messages_to_persist.append(tool_user_msg)
            continue  # 下一轮 LLM

        # 未知 stop_reason
        yield {"type": "message_end", "stop_reason": response.stop_reason}
        break
    else:
        # 走到 for 的 else 说明达到 10 轮上限
        yield {"type": "error", "message": "Agent loop exceeded 10 iterations"}

    # 持久化新消息到 draft.chat_history（QA stateless 模式跳过）
    if draft_id is not None and new_messages_to_persist:
        await append_draft_messages(db, draft_id, new_messages_to_persist)


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
    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in run_agent(
                body.message, draft_id, ctx, db,
                agent_role="employee_submit",
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


async def compose_explanation(
    submission_id: str, ctx: UserContext, db: AsyncSession,
) -> dict:
    """嵌入式 AI 解释卡的核心逻辑——单次"agent 运行"。

    架构诚实性：当前 Mock 实现是 deterministic workflow（固定调 2 个 tool
    + 规则化组合）。Real LLM (Day 5) 接上时，会用同样的 tool 白名单，
    但由 LLM 自己决定调哪个 tool / 组合什么文案 —— 那时才是真 agent。

    手工跑 agent loop 的关键：用 TOOL_REGISTRY['manager_explain'] 强制
    白名单，所有 tool 调用都过 TOOL_HANDLERS，和 run_agent 用同一套基础设施。
    """
    role = "manager_explain"
    allowed = set(TOOL_REGISTRY.get(role, []))

    async def call_tool(name: str, args: dict) -> dict:
        if name not in allowed:
            return {"error": f"tool '{name}' not allowed for role '{role}'"}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return {"error": f"unknown tool {name}"}
        try:
            return await handler(args, ctx, db, "")
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    # ── Step 1: 拉报销单本体 + audit_report ──
    sub = await call_tool("get_submission_for_review", {"submission_id": submission_id})
    if sub.get("error"):
        return {"error": sub["error"]}

    # ── Step 2: 拉该员工最近 10 笔历史 ──
    history = await call_tool("get_employee_submission_history",
                              {"employee_id": sub["employee_id"], "limit": 10})

    # ── Step 3: 规则化组合（Mock 阶段；RealLLM 接上后由 LLM 写）──
    tier = sub.get("tier") or "T2"
    risk = sub.get("risk_score") or 50.0
    audit = sub.get("audit_report") or {}
    timeline = audit.get("timeline") or []
    shield = audit.get("shield_report") or {}

    green: list[str] = []
    yellow: list[str] = []
    red: list[str] = []

    # ── 从 5-skill timeline 推 flags ──
    for step in timeline:
        msg = step.get("message", "")
        if step.get("passed"):
            if msg:
                green.append(msg)
        elif not step.get("skipped"):
            red.append(msg)

    # ── 字段完整性 ──
    if sub.get("invoice_number"):
        green.append(f"发票号已识别 ({sub['invoice_number']})")
    else:
        yellow.append("缺少发票号")

    if sub.get("description") and len(sub["description"]) >= 10:
        green.append("费用描述具体，包含场景信息")
    elif sub.get("description"):
        yellow.append(f"费用描述较短：『{sub['description']}』")
    else:
        yellow.append("缺少费用描述")

    # ── 金额对比员工历史 ──
    context = {}
    if history and not history.get("error") and history.get("items"):
        items = history["items"]
        amounts = [it["amount"] for it in items if it["id"] != sub["id"][:8]]
        if amounts:
            avg = sum(amounts) / len(amounts)
            this = sub["amount"]
            if this <= avg * 0.8:
                green.append(f"金额 ¥{this:.0f} 低于该员工平均 ¥{avg:.0f}")
            elif this >= avg * 1.5:
                yellow.append(f"金额 ¥{this:.0f} 显著高于该员工平均 ¥{avg:.0f}")
            else:
                green.append(f"金额 ¥{this:.0f} 接近该员工平均 ¥{avg:.0f}")
            context = {
                "history_count": len(items),
                "history_avg": round(avg, 2),
                "this_vs_avg_pct": round((this / avg - 1) * 100, 1) if avg else 0,
            }
        else:
            context = {"history_count": len(items), "history_avg": None}
    else:
        context = {"history_count": 0, "history_avg": None}

    # ── 从 ambiguity shield_report 拉风险信号 ──
    if shield:
        shield_score = shield.get("total_score") or shield.get("risk_score") or 0
        if shield_score >= 30:
            red.append(f"模糊性检测分 {shield_score} ≥ 30（高）")
        for signal in (shield.get("triggered") or shield.get("signals") or [])[:3]:
            if isinstance(signal, dict):
                yellow.append(signal.get("message") or signal.get("name") or str(signal))
            else:
                yellow.append(str(signal))

    # ── 推荐 ──
    if tier == "T1":
        recommendation = "approve"
        headline = "建议批准（低风险）"
    elif tier == "T2":
        recommendation = "approve"
        headline = "建议批准（次低风险）"
    elif tier == "T3":
        recommendation = "review"
        headline = "建议人工复核（中风险）"
    else:  # T4
        recommendation = "reject"
        headline = "建议驳回（高风险）"

    # ── advisory：软指引 ──
    advisory = None
    if tier in ("T1", "T2") and yellow:
        advisory = f"可批，但建议提醒员工：{yellow[0]}"
    elif tier == "T3":
        advisory = "需人工核对一次发票原件再决定"
    elif tier == "T4":
        advisory = "建议驳回并要求员工重新提交完整证据"

    # ── Cite the rule: structured rule violations from audit_report ──
    # Each entry is {rule_id, rule_text, severity, suggestion?, evidence?}.
    # Sourced from audit_report.violations (built by ExpenseController and
    # the submit handler — see agent/violation_registry.py for the catalog).
    violations = audit.get("violations") or []

    # ── Layer-2 investigator output (OODA agent — only present when
    # combined_risk >= 80 fired the trigger in _run_pipeline). Pass
    # through verbatim; the AI explanation card renders it as its own
    # section with verdict badge + evidence chain + summary.
    investigation = audit.get("investigation")

    return {
        "submission_id": sub["id"],
        "tier": tier,
        "risk_score": risk,
        "recommendation": recommendation,
        "headline": headline,
        "summary": {
            "merchant": sub.get("merchant"),
            "amount": sub.get("amount"),
            "currency": sub.get("currency"),
            "category": sub.get("category"),
            "date": sub.get("date"),
        },
        "green_flags": green[:5],
        "yellow_flags": yellow[:5],
        "red_flags": red[:5],
        "violations": violations,
        "investigation": investigation,
        "advisory": advisory,
        "context": context,
        "_agent_role": role,
        "_tools_called": ["get_submission_for_review", "get_employee_submission_history"],
    }


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
    """Unified drawer chat — single endpoint, role chosen by ctx.role.

    Routing: backend reads ``ctx.role`` and picks the agent_role + tool
    whitelist; the client cannot escalate by sending a role parameter.
      - ``employee`` / ``employee_submit`` → owner-scoped read + write
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
        agent_role = "employee"

    messages_for_agent = list(body.messages or [])

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