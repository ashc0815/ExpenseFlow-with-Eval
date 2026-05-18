"""Skill 03: 合规检查——整个系统的灵魂。

对每条 line_item:
1. 通过 CityNormalizer 标准化城市名
2. 通过 PolicyEngine 获取该员工等级+该城市等级对应的限额
3. 比对金额 vs 限额 → A/B/C 级
4. 检查 expense_types.yaml 中的附加要求（如招待费参会人名单）
5. 无法明确判断的 → 转交 AmbiguityDetector

汇总逻辑:
- 全 A → 整单 A
- 有 B 无 C → 整单 B
- 有 C → 整单 C
- 有 AMBIGUOUS → shield_triggered=True
"""

from __future__ import annotations

from typing import Optional

from config import ConfigLoader
from agent.ambiguity_detector import AmbiguityDetector
from backend.services.field_source_trust import field_source_trust_score
from models.enums import ComplianceLevel
from models.expense import (
    AmbiguityResult,
    ComplianceResult,
    Employee,
    ExpenseReport,
    LineItem,
    LineItemComplianceDetail,
    RuleResult,
)
from rules.policy_engine import PolicyEngine


SKILL_NAME = "compliance"


# ------------------------------------------------------------------
# 核心 API
# ------------------------------------------------------------------

def process(
    report: ExpenseReport,
    employee: Optional[Employee] = None,
    history: Optional[list[LineItem]] = None,
) -> ComplianceResult:
    """对报销单执行逐行合规检查。

    Args:
        report: 待检查的报销单。
        employee: 报销人（默认从 report.employee 取）。
        history: 该员工的历史行项目（用于模式异常检测）。

    Returns:
        ComplianceResult，含整单等级、逐行明细和 shield 状态。
    """
    if employee is None:
        employee = report.employee
    if history is None:
        history = []

    loader = ConfigLoader()
    engine = PolicyEngine(loader)
    normalizer = engine.city_normalizer
    detector = AmbiguityDetector(loader)
    trust_config = (loader.get("policy") or {}).get("field_source_trust", {})

    line_details: list[LineItemComplianceDetail] = []
    all_levels: list[ComplianceLevel] = []
    shield_triggered = False
    issues: list[str] = []

    for idx, item in enumerate(report.line_items):
        # ---- 1. 城市标准化 ----
        normalized_city = normalizer.normalize(item.city)
        city_tier = normalizer.get_tier(item.city)

        # ---- 2. 获取限额 ----
        subtype_cfg = engine.get_subtype_config(item.expense_type)
        limit_key = subtype_cfg.get("limit_key")
        limit: Optional[float] = None
        if limit_key:
            limit = engine.get_limit(limit_key, item.city, employee.level.value)

        # ---- 3. 判定 A/B/C ----
        if limit is None:
            level = ComplianceLevel.A
        else:
            level = engine.check_tolerance(item.amount, limit)
        all_levels.append(level)

        if level == ComplianceLevel.B:
            issues.append(
                f"行[{idx}] {item.expense_type}: ¥{item.amount} 超标但在容忍度内"
                f"(限额¥{limit})"
            )
            if item.field_sources:
                amount_source = item.field_sources.get("amount", "unknown")
                normalized_source, trust_score = field_source_trust_score(
                    amount_source,
                    trust_config,
                )
                if trust_score < 0.7:
                    issues.append(
                        f"行[{idx}] 金额来源为{amount_source}"
                        f"(归一化={normalized_source}, 信任度{trust_score:.1f})，"
                        "且金额处于边界区间，建议人工复核金额准确性"
                    )
        elif level == ComplianceLevel.C:
            issues.append(
                f"行[{idx}] {item.expense_type}: ¥{item.amount} 超标拒绝"
                f"(限额¥{limit})"
            )

        # ---- 4. 附加校验 ----
        extra_checks = _check_extra_requirements(item, subtype_cfg, idx)
        for chk in extra_checks:
            if not chk.passed:
                issues.append(f"行[{idx}] {chk.rule_name}: {chk.message}")

        # ---- 5. 模糊检测 ----
        ambiguity: Optional[AmbiguityResult] = None
        ambiguity = detector.evaluate(item, employee, extra_checks, history)
        if ambiguity.recommendation != "auto_pass":
            shield_triggered = True
            issues.append(
                f"行[{idx}] 模糊检测({ambiguity.score:.0f}分): {ambiguity.explanation}"
            )

        line_details.append(LineItemComplianceDetail(
            line_item=item,
            normalized_city=normalized_city,
            city_tier=city_tier,
            limit=limit,
            compliance_level=level,
            extra_checks=extra_checks,
            ambiguity=ambiguity,
        ))

    # ---- 汇总整单等级 ----
    if any(lv == ComplianceLevel.C for lv in all_levels):
        overall = ComplianceLevel.C
    elif any(lv == ComplianceLevel.B for lv in all_levels):
        overall = ComplianceLevel.B
    else:
        overall = ComplianceLevel.A

    return ComplianceResult(
        overall_level=overall,
        line_details=line_details,
        shield_triggered=shield_triggered,
        issues=issues,
    )


# ------------------------------------------------------------------
# Controller 兼容入口
# ------------------------------------------------------------------

def process_report(report: ExpenseReport, config: dict) -> dict:
    """AgentController 调用入口。

    Returns:
        {"passed": bool, "compliance_result": ComplianceResult, "issues": list[str]}
    """
    result = process(report)

    # C 级拒绝 → 不通过
    passed = result.overall_level != ComplianceLevel.C

    log_detail = (
        f"整单{result.overall_level.value}级, "
        f"shield={'触发' if result.shield_triggered else '未触发'}, "
        f"{len(result.issues)}个问题"
    )
    report.add_log(SKILL_NAME, "pass" if passed else "fail", log_detail)

    return {
        "passed": passed,
        "compliance_result": result,
        "issues": result.issues,
    }


# ------------------------------------------------------------------
# 内部函数
# ------------------------------------------------------------------

def _check_extra_requirements(
    item: LineItem, subtype_cfg: dict, idx: int,
) -> list[RuleResult]:
    """检查 expense_types.yaml 中定义的附加要求。"""
    checks: list[RuleResult] = []

    # 招待费必须附参会人员名单
    if subtype_cfg.get("requires_attendee_list"):
        has_attendees = bool(item.attendees)
        checks.append(RuleResult(
            rule_name="attendee_list_required",
            passed=has_attendees,
            message="招待费缺少参会人员名单" if not has_attendees else "参会人员名单已提供",
            severity="error" if not has_attendees else "info",
        ))

    # 要求发票但未提供
    if subtype_cfg.get("requires_invoice") and item.invoice is None:
        checks.append(RuleResult(
            rule_name="invoice_required",
            passed=False,
            message=f"费用类型 '{item.expense_type}' 要求提供发票",
            severity="error",
        ))

    return checks
