"""Concrete tool handlers for ExpenseFlow agent tools."""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext
from backend.db.store import (
    create_audit_log,
    get_draft,
    get_employee,
    get_report,
    get_submission,
    get_submission_by_invoice,
    list_submissions,
    update_draft_field as store_update_draft_field,
)
from backend.services.didi_provider import lookup_didi_trip as lookup_didi_trip_provider
from backend.services.external_evidence_fixtures import lookup_fixture_evidence
from backend.services.injection_guard import scan_text

from .llm import _clean_llm_env_value

_ALLOWED_FIELDS = {
    "merchant", "amount", "date", "category", "tax_amount",
    "invoice_number", "invoice_code", "project_code", "description",
    "currency",
}

async def _gpt4o_ocr(receipt_url: str) -> Optional[dict]:
    """GPT-4o Vision 识别发票图片，返回字段 dict 或 None（失败时回退 mock）。

    receipt_url 形如 /uploads/YYYY-MM/uuid_name.jpg（LocalStorage 格式）。
    Records an LLM trace on every attempt (success or failure).
    """
    import base64
    from openai import AsyncOpenAI
    from backend.services.trace import record_trace, TraceTimer

    api_key = _clean_llm_env_value("OPENAI_API_KEY")
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
    model = _clean_llm_env_value("OPENAI_MODEL", "gpt-4o") or "gpt-4o"
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


def _redact_ocr_injection(result: dict) -> dict:
    """Scan OCR free text before it enters agent history."""
    raw_text_fields = [
        result.get("merchant", ""),
        result.get("description", ""),
        result.get("items_text", ""),
        result.get("remarks", ""),
    ]
    for item in result.get("items") or []:
        if isinstance(item, dict):
            raw_text_fields.append(item.get("description", ""))

    combined_text = " ".join(str(field) for field in raw_text_fields if field)
    injection_report = scan_text(combined_text)
    if not injection_report:
        return result

    redacted = dict(result)
    redacted["_injection_warning"] = True
    redacted["_injection_patterns"] = injection_report["patterns"]
    risk_flags = list(redacted.get("risk_flags") or [])
    if "prompt_injection" not in risk_flags:
        risk_flags.append("prompt_injection")
    redacted["risk_flags"] = risk_flags

    for field_name in ("description", "remarks", "items_text"):
        if field_name in redacted and redacted[field_name]:
            redacted[field_name] = "[REDACTED - injection pattern detected]"

    if isinstance(redacted.get("items"), list):
        items = []
        for item in redacted["items"]:
            if isinstance(item, dict):
                scrubbed = dict(item)
                if scrubbed.get("description"):
                    scrubbed["description"] = "[REDACTED - injection pattern detected]"
                items.append(scrubbed)
            else:
                items.append(item)
        redacted["items"] = items
    return redacted


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
            return _redact_ocr_injection(ocr_result)

    # ── Mock 数据（金色路径设计，无 API Key 时使用）───────────────
    import random
    from datetime import timedelta
    invoice_number = f"{random.randint(10000000, 99999999)}"

    today = date.today()
    d = today
    while d.weekday() >= 5:  # 回退到最近工作日
        d -= timedelta(days=1)

    return _redact_ocr_injection({
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
    })


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
    if str(invoice_number).strip().upper() == "INV-DUP-001":
        return {
            "is_duplicate": True,
            "existing_submission_id": "mock-duplicate-inv-dup-001",
            "submitted_by": ctx.user_id,
            "submitted_at": "2026-05-07T09:30:00+00:00",
            "source": "duplicate_invoice_fixture",
        }
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
        # Backward-compatible aliases for older tests/clients that predate
        # home-currency support.
        "total": round(total_home, 2),
        "total_cny": round(total_home, 2) if home_cur == "CNY" else None,
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
    updated = await store_update_draft_field(db, draft_id, field, value, source=source)
    if not updated:
        return {"error": "当前没有可写入的报销草稿"}
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
            "via": "expense_assistant_drawer",
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
    limit_labels: dict[str, str] = {}
    expense_categories_struct = []
    for cat_id, cat in types.get("expense_types", {}).items():
        for sub in cat.get("subtypes", []):
            limit_key = sub.get("limit_key")
            if limit_key and limit_key not in limit_labels:
                limit_labels[limit_key] = sub.get("name_zh") or limit_key.replace("_", " ")
            expense_categories_struct.append({
                "category": cat.get("name_zh", cat_id),
                "subtype": sub.get("name_zh", sub.get("id", "")),
                "requires_invoice": bool(sub.get("requires_invoice")),
                "requires_attendee_list": bool(sub.get("requires_attendee_list")),
                "limit_key": limit_key,
            })

    limit_matrix = []
    for key, tiers in limits.items():
        name = limit_labels.get(key, key.replace("_", " "))
        for tier, levels in tiers.items():
            vals = ", ".join(f"{lv}: ¥{v}" if v != "不限" else f"{lv}: 不限" for lv, v in levels.items())
            limit_text.append(f"{name} ({tier}): {vals}")
            row = {
                "key": key,
                "name": name,
                "tier": tier,
            }
            row.update(levels or {})
            limit_matrix.append(row)

    expense_cats = []
    for item in expense_categories_struct:
        flags = []
        if item.get("requires_invoice"):
            flags.append("需发票")
        if item.get("requires_attendee_list"):
            flags.append("需参会人员名单")
        expense_cats.append(f"{item['category']}/{item['subtype']} — {'、'.join(flags) if flags else '无特殊要求'}")

    city_tiers = policy.get("city_tiers", {})
    city_info = []
    city_tiers_struct = []
    for tier, data in city_tiers.items():
        cities = data.get("cities", [])
        city_info.append(f"{tier}: {', '.join(str(c) for c in cities)}")
        city_tiers_struct.append({"tier": tier, "cities": cities})

    payment = policy.get("payment", {})
    tolerance = policy.get("tolerance", {})

    return {
        "company": policy.get("company_info", {}).get("name", ""),
        "employee_levels": [lv["id"] + " " + lv["name"] for lv in policy.get("employee_levels", [])],
        "employee_levels_struct": policy.get("employee_levels", []),
        "city_tiers": city_info,
        "city_tiers_struct": city_tiers_struct,
        "limits": limit_text,
        "limit_matrix": limit_matrix,
        "expense_categories": expense_cats,
        "expense_categories_struct": expense_categories_struct,
        "payment_rules": {
            "bank_transfer_threshold": f"≥¥{payment.get('bank_transfer_threshold', 5000)} 走银行转账",
            "petty_cash_max": f"<¥{payment.get('petty_cash_max', 5000)} 可走备用金",
        },
        "tolerance_rules": {
            "warning_threshold": f"超标 ≤¥{tolerance.get('warning_threshold', 50)} 为警告（可通过）",
            "reject_above": f"超标 >¥{tolerance.get('reject_above', 50)} 为拒绝",
        },
    }


async def tool_lookup_ctrip_booking(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """Mockable read-only Trip.com/Ctrip booking lookup.

    Local/eval mode reads deterministic YAML fixtures from demo_receipt. Tests
    can select data by fixture_id or case_id, while normal dev queries use a
    small default fixture subset.
    """
    return lookup_fixture_evidence("ctrip", args)


async def tool_lookup_didi_trip(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """Read-only Didi trip lookup.

    Defaults to deterministic local fixtures for eval reproducibility.
    Set ``DIDI_PROVIDER=mcp_sandbox`` plus ``DIDI_MCP_KEY`` or
    ``DIDI_MCP_URL`` to call Didi's real MCP sandbox endpoint.
    """
    return await lookup_didi_trip_provider(args)


async def tool_lookup_card_transaction(
    args: dict, ctx: UserContext, db: AsyncSession, draft_id: str
) -> dict:
    """Mockable read-only corporate card transaction lookup."""
    return lookup_fixture_evidence("card", args)


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
    "lookup_ctrip_booking":              tool_lookup_ctrip_booking,
    "lookup_didi_trip":                  tool_lookup_didi_trip,
    "lookup_card_transaction":           tool_lookup_card_transaction,
    "get_pending_approval_queue":        tool_get_pending_approval_queue,
    "get_team_spend_summary":            tool_get_team_spend_summary,
}
