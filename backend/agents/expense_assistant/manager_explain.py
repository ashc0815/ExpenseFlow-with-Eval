"""Manager/finance explanation workflow for reviewed submissions."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext

from .manifest import TOOL_REGISTRY
from .tool_handlers import TOOL_HANDLERS

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

