"""模糊/歧义检测器——识别需要人工复核的边界情况。

五维评分模型（加权求和 0-100）：
1. 描述模糊度 (25%): description <10字 或包含泛化词
2. 金额边界   (20%): 金额在限额的 90%-110% 区间
3. 模式异常   (25%): 同一员工 7天内 ≥3笔相似金额(±15%)的同类型费用
4. 时间异常   (15%): 周末/节假日的工作餐、非工作时间的交通费
5. 城市不匹配 (15%): 原始城市名未被识别，或标准化前后不一致

ambiguity_score:
  <30  → auto_pass（自动通过，附低风险标签）
  30-70 → human_review（标记待人工复核）
  >70  → suggest_reject（建议拒绝）

当 score > 50 时，调用 Claude API 做深度语义分析。
无 ANTHROPIC_API_KEY 时自动 fallback 到规则评分模型。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Optional

from config import ConfigLoader
from backend.services.field_source_trust import field_source_trust_score
from models.expense import AmbiguityResult, Employee, LLMReviewResult, LineItem, RuleResult
from rules.policy_engine import PolicyEngine


logger = logging.getLogger(__name__)

# Fallback constants — used only when the shared config loader fails or
# returns empty values. The dashboard-editable source of truth lives in
# backend/tests/eval_config.json (see backend.services.config_loader).

_FALLBACK_VAGUE_WORDS: list[str] = [
    "相关费用", "其他", "杂项", "若干", "一批", "等等",
    "费用", "相关", "补贴", "报销",
]

_FALLBACK_WEIGHTS = {
    "description_vague": 0.25,
    "amount_boundary":   0.20,
    "pattern_anomaly":   0.25,
    "time_anomaly":      0.15,
    "city_mismatch":     0.15,
}

_FALLBACK_LLM_TRIGGER_THRESHOLD = 50

# Score boundaries: < AUTO → auto_pass, <= REVIEW → human_review,
# > REVIEW → suggest_reject. Defaults match the historical hardcoded values.
_FALLBACK_TIER_AUTO_PASS_MAX = 30
_FALLBACK_TIER_HUMAN_REVIEW_MAX = 70

_FALLBACK_DESC_SHORT_THRESHOLD_CHARS = 10
_FALLBACK_DESC_SHORT_SCORE = 50.0

_FALLBACK_BOUNDARY_LOWER_PCT = 0.90
_FALLBACK_BOUNDARY_UPPER_PCT = 1.10

# Fallback template — used only when eval_prompts.json has no ambiguity_llm entry.
# The dashboard-editable version lives in backend/tests/eval_prompts.json under
# key `ambiguity_llm`. Uses {placeholder} syntax substituted via str.replace
# (not str.format, to avoid conflicts with the literal {...} JSON example).
_FALLBACK_AMBIGUITY_TEMPLATE = """你是企业财务合规审计专家。以下费用明细触发了模糊判定。

员工信息：{employee_name}（等级：{employee_level}，部门：{department}）
费用明细：
  - 类型: {expense_type}
  - 金额: ¥{amount}
  - 城市: {city} → {normalized_city}
  - 日期: {date}
  - 描述: {description}
  - 参会人: {attendees}
适用限额：{limit_display}（城市等级：{city_tier}，员工等级：{employee_level}）
规则引擎结果：{rule_summary}
模糊触发因素：{triggered_factors}（{explanation}）

请分析：
1. 合规风险等级：高/中/低
2. 具体风险点
3. 建议：通过 / 退回补充材料 / 拒绝
4. 退回时需补充的材料清单

严格按以下JSON格式返回，不要包含其他文本：
{"risk_level": "高/中/低", "risk_points": ["风险点1"], "recommendation": "approve/review/reject", "reasoning": "分析说明", "required_materials": ["材料1"]}"""


def _load_weights() -> dict[str, float]:
    """Load ambiguity weights from eval_config.json, fallback to hardcoded."""
    try:
        from backend.services.config_loader import load_ambiguity_weights
        w = load_ambiguity_weights()
        if w:
            return w
    except Exception:
        pass
    return dict(_FALLBACK_WEIGHTS)


def _load_trigger_threshold() -> int:
    """Load LLM trigger threshold from eval_config.json, fallback to hardcoded."""
    try:
        from backend.services.config_loader import load_ambiguity_trigger_threshold
        return load_ambiguity_trigger_threshold()
    except Exception:
        return _FALLBACK_LLM_TRIGGER_THRESHOLD


def _load_tier_thresholds() -> tuple[int, int]:
    try:
        from backend.services.config_loader import load_ambiguity_tier_thresholds
        t = load_ambiguity_tier_thresholds()
        return (
            int(t.get("auto_pass_max", _FALLBACK_TIER_AUTO_PASS_MAX)),
            int(t.get("human_review_max", _FALLBACK_TIER_HUMAN_REVIEW_MAX)),
        )
    except Exception:
        return _FALLBACK_TIER_AUTO_PASS_MAX, _FALLBACK_TIER_HUMAN_REVIEW_MAX


def _load_vague_words() -> list[str]:
    try:
        from backend.services.config_loader import load_ambiguity_vague_words
        words = load_ambiguity_vague_words()
        if words:
            return words
    except Exception:
        pass
    return list(_FALLBACK_VAGUE_WORDS)


def _load_description_short() -> tuple[int, float]:
    try:
        from backend.services.config_loader import load_ambiguity_description_short
        return load_ambiguity_description_short()
    except Exception:
        return (
            _FALLBACK_DESC_SHORT_THRESHOLD_CHARS,
            _FALLBACK_DESC_SHORT_SCORE,
        )


def _load_boundary_band() -> tuple[float, float]:
    try:
        from backend.services.config_loader import load_ambiguity_boundary_band
        return load_ambiguity_boundary_band()
    except Exception:
        return (_FALLBACK_BOUNDARY_LOWER_PCT, _FALLBACK_BOUNDARY_UPPER_PCT)


# 因素权重 — loaded from shared config
_WEIGHTS = _load_weights()

# LLM 触发阈值 — loaded from shared config
_LLM_TRIGGER_THRESHOLD = _load_trigger_threshold()

# Per-tier risk score boundaries (auto_pass / human_review / suggest_reject)
_TIER_AUTO_PASS_MAX, _TIER_HUMAN_REVIEW_MAX = _load_tier_thresholds()

# Words / lengths / bands previously hardcoded in the scoring fns
_VAGUE_WORDS: list[str] = _load_vague_words()
_DESC_SHORT_THRESHOLD, _DESC_SHORT_SCORE = _load_description_short()
_BOUNDARY_LOWER_PCT, _BOUNDARY_UPPER_PCT = _load_boundary_band()


class AmbiguityDetector:
    """检测报销行项目中的模糊或歧义信息。"""

    def __init__(self, config_loader: ConfigLoader) -> None:
        self._loader = config_loader
        self._engine = PolicyEngine(config_loader)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        line_item: LineItem,
        employee: Employee,
        rule_results: list[RuleResult],
        history: list[LineItem],
        fraud_signals: Optional[list[dict]] = None,
    ) -> AmbiguityResult:
        """对单行项目进行模糊条件评分。

        Args:
            line_item: 待评估的行项目。
            employee: 报销人。
            rule_results: 前序合规检查的 RuleResult 列表（供参考）。
            history: 同一员工的历史行项目（用于模式异常检测）。

        Returns:
            AmbiguityResult，含 0-100 评分、触发因素列表、建议和中文解释。
            当 score > 50 时额外包含 llm_review 结果。
        """
        factors: dict[str, float] = {}
        triggered: list[str] = []
        explanations: list[str] = []

        # ---- 1. 描述模糊度 (25%) ----
        score_desc = self._score_description(line_item.description)
        factors["description_vague"] = score_desc
        if score_desc > 0:
            triggered.append("description_vague")
            if len(line_item.description) < _DESC_SHORT_THRESHOLD:
                explanations.append(f"描述过短({len(line_item.description)}字)")
            else:
                explanations.append("描述含泛化词")

        # ---- 2. 金额边界 (20%) ----
        score_boundary = self._score_amount_boundary(line_item, employee)
        factors["amount_boundary"] = score_boundary
        low_trust_boundary_boost = 0.0
        if score_boundary > 0:
            triggered.append("amount_boundary")
            explanations.append("金额处于限额边界(90%-110%)")
            field_sources = getattr(line_item, "field_sources", {}) or {}
            if "amount" in field_sources:
                trust_config = (self._loader.get("policy") or {}).get("field_source_trust", {})
                amount_source = field_sources.get("amount")
                normalized_source, source_trust = field_source_trust_score(
                    amount_source,
                    trust_config,
                )
                if source_trust < 0.7:
                    low_trust_boundary_boost = 10.0
                    triggered.append("field_source_trust")
                    explanations.append(
                        f"金额来源{amount_source}(归一化={normalized_source}, 信任度{source_trust:.1f})"
                    )

        # ---- 3. 模式异常 (25%) ----
        score_pattern = self._score_pattern_anomaly(line_item, history)
        factors["pattern_anomaly"] = score_pattern
        if score_pattern > 0:
            triggered.append("pattern_anomaly")
            explanations.append("7天内存在多笔相似金额的同类型费用")

        # ---- 4. 时间异常 (15%) ----
        score_time = self._score_time_anomaly(line_item)
        factors["time_anomaly"] = score_time
        if score_time > 0:
            triggered.append("time_anomaly")
            day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][line_item.date.weekday()]
            explanations.append(f"费用发生在{day_name}")

        # ---- 5. 城市不匹配 (15%) ----
        score_city = self._score_city_mismatch(line_item)
        factors["city_mismatch"] = score_city
        if score_city > 0:
            triggered.append("city_mismatch")
            normalized = self._engine.city_normalizer.normalize(line_item.city)
            if not self._engine.city_normalizer.is_known(line_item.city):
                explanations.append(f"城市'{line_item.city}'无法识别")
            else:
                explanations.append(f"城市名标准化: '{line_item.city}'→'{normalized}'")

        # ---- 加权汇总 ----
        total_score = sum(
            factors[k] * _WEIGHTS[k] for k in _WEIGHTS
        )
        total_score += low_trust_boundary_boost
        total_score = round(min(100.0, max(0.0, total_score)), 1)

        # ---- 建议 ----
        if total_score < _TIER_AUTO_PASS_MAX:
            recommendation = "auto_pass"
        elif total_score <= _TIER_HUMAN_REVIEW_MAX:
            recommendation = "human_review"
        else:
            recommendation = "suggest_reject"

        explanation = "; ".join(explanations) if explanations else "未发现异常"

        # ---- LLM 深度分析（score > 50 触发） ----
        llm_result: Optional[LLMReviewResult] = None
        if total_score > _LLM_TRIGGER_THRESHOLD:
            llm_result = self._run_llm_review(
                line_item, employee, rule_results, triggered, explanation,
            )
            # LLM 结果可能升级 recommendation
            if llm_result and llm_result.recommendation == "reject" and recommendation != "suggest_reject":
                recommendation = "suggest_reject"

        return AmbiguityResult(
            score=total_score,
            triggered_factors=triggered,
            recommendation=recommendation,
            explanation=explanation,
            llm_review=llm_result,
        )

    # ------------------------------------------------------------------
    # LLM 审核
    # ------------------------------------------------------------------

    def _run_llm_review(
        self,
        line_item: LineItem,
        employee: Employee,
        rule_results: list[RuleResult],
        triggered_factors: list[str],
        explanation: str,
    ) -> LLMReviewResult:
        """调用 LLM 做深度语义分析。

        提供商优先级: MiniMax → Claude → fallback 规则模型。
        - MINIMAX_API_KEY 存在 → 使用 MiniMax（OpenAI 兼容接口）
        - ANTHROPIC_API_KEY 存在 → 使用 Claude
        - 都没有 → fallback 到规则评分模型
        """
        prompt = self._build_llm_prompt(
            line_item, employee, rule_results, triggered_factors, explanation,
        )

        # 1. 尝试 MiniMax
        minimax_key = os.environ.get("MINIMAX_API_KEY", "")
        if minimax_key:
            result = self._call_minimax(prompt, minimax_key)
            if result is not None:
                return result

        # 2. 尝试 Claude
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if anthropic_key:
            result = self._call_claude(prompt, anthropic_key)
            if result is not None:
                return result

        # 3. Fallback 规则模型
        logger.info("未配置可用 LLM API key，使用 fallback 规则评分模型")
        return self._fallback_llm_review(
            line_item, employee, triggered_factors, explanation,
        )

    def _build_llm_prompt(
        self,
        line_item: LineItem,
        employee: Employee,
        rule_results: list[RuleResult],
        triggered_factors: list[str],
        explanation: str,
    ) -> str:
        """构建发送给 LLM 的合规审计 prompt。

        Template comes from eval_prompts.json (key `ambiguity_llm`) so the
        Eval Dashboard can edit it without a code change. Falls back to
        _FALLBACK_AMBIGUITY_TEMPLATE when the JSON entry is missing.
        """
        try:
            from backend.services.config_loader import load_prompt
            template = load_prompt("ambiguity_llm") or _FALLBACK_AMBIGUITY_TEMPLATE
        except Exception:
            template = _FALLBACK_AMBIGUITY_TEMPLATE

        normalizer = self._engine.city_normalizer
        normalized_city = normalizer.normalize(line_item.city)
        city_tier = normalizer.get_tier(line_item.city)
        subtype_cfg = self._engine.get_subtype_config(line_item.expense_type)
        limit_key = subtype_cfg.get("limit_key", "")
        limit = self._engine.get_limit(limit_key, line_item.city, employee.level.value) if limit_key else None
        limit_display = f"¥{limit:.0f}" if limit else "不限"

        rule_summary = "; ".join(
            f"{r.rule_name}: {'通过' if r.passed else '未通过'} - {r.message}"
            for r in rule_results
        ) if rule_results else "无前序规则结果"

        substitutions = {
            "{employee_name}":     employee.name,
            "{employee_level}":    employee.level.value,
            "{department}":        employee.department,
            "{expense_type}":      line_item.expense_type,
            "{amount}":            str(line_item.amount),
            "{city}":              line_item.city,
            "{normalized_city}":   normalized_city,
            "{date}":              str(line_item.date),
            "{description}":       line_item.description or "",
            "{attendees}":         ", ".join(line_item.attendees) if line_item.attendees else "未提供",
            "{limit_display}":     limit_display,
            "{city_tier}":         str(city_tier),
            "{rule_summary}":      rule_summary,
            "{triggered_factors}": ", ".join(triggered_factors),
            "{explanation}":       explanation,
        }
        out = template
        for key, val in substitutions.items():
            out = out.replace(key, val)
        return out

    def _call_minimax(self, prompt: str, api_key: str) -> Optional[LLMReviewResult]:
        """通过 OpenAI 兼容 SDK 调用 MiniMax M2。

        环境变量:
            MINIMAX_API_KEY: 必需
            MINIMAX_BASE_URL: 可选，默认 https://api.minimaxi.com/v1
            MINIMAX_MODEL:    可选，默认 MiniMax-M2
        """
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("openai 包未安装，无法调用 MiniMax。请运行: pip install openai")
            return None

        from backend.services.trace import record_trace_sync, TraceTimer

        base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
        model = os.environ.get("MINIMAX_MODEL", "MiniMax-M2")
        messages = [{"role": "user", "content": prompt}]

        raw_text = ""
        usage: Optional[dict] = None
        err: Optional[str] = None
        timer = TraceTimer()
        try:
            with timer:
                client = OpenAI(api_key=api_key, base_url=base_url)
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=1024,
                )
            raw_text = (resp.choices[0].message.content or "").strip()
            if getattr(resp, "usage", None):
                usage = {"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens}
            logger.info(f"MiniMax 返回: {len(raw_text)} 字符")
            return self._parse_llm_response(raw_text, source="minimax")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"MiniMax API 调用失败: {e}")
            return None
        finally:
            record_trace_sync(
                component="ambiguity_detector",
                model=f"minimax/{model}",
                prompt=messages,
                response=raw_text or None,
                latency_ms=timer.elapsed_ms or None,
                token_usage=usage,
                error=err,
            )

    def _call_claude(self, prompt: str, api_key: str) -> Optional[LLMReviewResult]:
        """通过 Anthropic SDK 调用 Claude。"""
        try:
            import anthropic
        except ImportError:
            logger.warning("anthropic 包未安装，无法调用 Claude。请运行: pip install anthropic")
            return None

        from backend.services.trace import record_trace_sync, TraceTimer

        model = "claude-sonnet-4-20250514"
        messages = [{"role": "user", "content": prompt}]

        raw_text = ""
        usage: Optional[dict] = None
        err: Optional[str] = None
        timer = TraceTimer()
        try:
            with timer:
                client = anthropic.Anthropic(api_key=api_key)
                message = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    messages=messages,
                )
            raw_text = message.content[0].text.strip()
            u = getattr(message, "usage", None)
            if u is not None:
                usage = {"input": u.input_tokens, "output": u.output_tokens}
            return self._parse_llm_response(raw_text, source="claude")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"Claude API 调用失败: {e}")
            return None
        finally:
            record_trace_sync(
                component="ambiguity_detector",
                model=model,
                prompt=messages,
                response=raw_text or None,
                latency_ms=timer.elapsed_ms or None,
                token_usage=usage,
                error=err,
            )

    def _parse_llm_response(self, raw_text: str, source: str = "claude") -> LLMReviewResult:
        """解析 LLM 返回的 JSON（通用于 MiniMax / Claude / 其他 OpenAI 兼容接口）。"""
        try:
            # 处理可能包含 markdown 代码块的情况
            text = raw_text
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            data = json.loads(text.strip())

            rec_map = {"approve": "approve", "review": "review", "reject": "reject"}
            recommendation = rec_map.get(data.get("recommendation", "review"), "review")

            return LLMReviewResult(
                confidence=0.9,
                recommendation=recommendation,
                reasoning=data.get("reasoning", ""),
                risk_level=data.get("risk_level", "中"),
                risk_points=data.get("risk_points", []),
                required_materials=data.get("required_materials", []),
                raw_response=raw_text,
                source=source,
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"{source} 返回解析失败: {e}")
            return LLMReviewResult(
                confidence=0.5,
                recommendation="review",
                reasoning=f"{source} 返回解析失败，原始内容: {raw_text[:200]}",
                risk_level="中",
                raw_response=raw_text,
                source=source,
            )

    def _fallback_llm_review(
        self,
        line_item: LineItem,
        employee: Employee,
        triggered_factors: list[str],
        explanation: str,
    ) -> LLMReviewResult:
        """无 API key 时的 fallback 规则评分模型。"""
        risk_points: list[str] = []
        required_materials: list[str] = []

        if "description_vague" in triggered_factors:
            risk_points.append("费用描述不清晰，无法确认业务合理性")
            required_materials.append("详细的费用用途说明")

        if "amount_boundary" in triggered_factors:
            risk_points.append("金额接近限额边界，存在凑额度嫌疑")

        if "pattern_anomaly" in triggered_factors:
            risk_points.append("短期内多笔相似金额，存在拆单报销嫌疑")
            required_materials.append("每笔费用的独立业务说明")

        if "time_anomaly" in triggered_factors:
            risk_points.append("非工作时间产生的费用，需确认业务必要性")
            required_materials.append("加班/周末工作审批记录")

        if "city_mismatch" in triggered_factors:
            risk_points.append("城市信息不一致或无法识别")
            required_materials.append("出差行程单或机票/车票凭证")

        # 检查招待费特殊要求
        subtype_cfg = self._engine.get_subtype_config(line_item.expense_type)
        if subtype_cfg.get("requires_attendee_list") and not line_item.attendees:
            risk_points.append("招待费缺少参会人员名单")
            required_materials.append("参会人员名单（含公司及职务）")

        n_factors = len(triggered_factors)
        if n_factors >= 4:
            risk_level = "高"
            recommendation = "reject"
        elif n_factors >= 2:
            risk_level = "中"
            recommendation = "review"
        else:
            risk_level = "低"
            recommendation = "approve"

        reasoning = (
            f"基于规则评分模型分析: 共触发{n_factors}个模糊因素"
            f"({', '.join(triggered_factors)}), "
            f"风险等级判定为{risk_level}。{explanation}"
        )

        return LLMReviewResult(
            confidence=0.7,
            recommendation=recommendation,
            reasoning=reasoning,
            risk_level=risk_level,
            risk_points=risk_points,
            required_materials=required_materials,
            raw_response="",
            source="fallback",
        )

    # ------------------------------------------------------------------
    # 评分因子
    # ------------------------------------------------------------------

    def _score_description(self, description: str) -> float:
        """描述模糊度评分。

        含泛化词 → 100分，过短（默认 <10字）→ short_score（默认 50），清晰 → 0分。
        Threshold and word list are loaded from eval_config.json so the
        eval dashboard can tune sensitivity per client.
        """
        if not description or not description.strip():
            return 100.0

        for word in _VAGUE_WORDS:
            if word in description:
                return 100.0

        if len(description.strip()) < _DESC_SHORT_THRESHOLD:
            return _DESC_SHORT_SCORE

        return 0.0

    def _score_amount_boundary(self, item: LineItem, employee: Employee) -> float:
        """金额边界评分。

        金额落在限额的 [lower_pct, upper_pct] 区间（默认 90%-110%）→ 100分。
        Band loaded from eval_config.json (tunable per client).
        """
        subtype_cfg = self._engine.get_subtype_config(item.expense_type)
        limit_key = subtype_cfg.get("limit_key")
        if not limit_key:
            return 0.0

        limit = self._engine.get_limit(limit_key, item.city, employee.level.value)
        if limit is None:
            return 0.0

        lower = limit * _BOUNDARY_LOWER_PCT
        upper = limit * _BOUNDARY_UPPER_PCT
        if lower <= item.amount <= upper:
            return 100.0
        return 0.0

    def _score_pattern_anomaly(self, item: LineItem, history: list[LineItem]) -> float:
        """模式异常评分。

        同一费用类型，7天内 ≥3笔金额在 ±15% 范围内 → 100分。
        """
        if not history:
            return 0.0

        window_start = item.date - timedelta(days=7)
        similar_count = 0
        for h in history:
            if h.expense_type != item.expense_type:
                continue
            if not (window_start <= h.date <= item.date):
                continue
            if item.amount == 0:
                continue
            ratio = abs(h.amount - item.amount) / item.amount
            if ratio <= 0.15:
                similar_count += 1

        if similar_count >= 3:
            return 100.0
        if similar_count == 2:
            return 50.0
        return 0.0

    def _score_time_anomaly(self, item: LineItem) -> float:
        """时间异常评分。

        周末(六/日)的餐费或交通费 → 100分。
        """
        weekday = item.date.weekday()  # 0=Mon, 5=Sat, 6=Sun
        is_weekend = weekday >= 5

        if not is_weekend:
            return 0.0

        # 只对餐费和交通费标记周末异常
        weekend_sensitive_types = {"meals", "transport_local", "client_meal"}
        if item.expense_type in weekend_sensitive_types:
            return 100.0

        return 0.0

    def _score_city_mismatch(self, item: LineItem) -> float:
        """城市不匹配评分。

        城市未识别 → 100分，标准化前后不一致 → 50分，完全匹配 → 0分。
        """
        normalizer = self._engine.city_normalizer
        if not normalizer.is_known(item.city):
            return 100.0

        normalized = normalizer.normalize(item.city)
        if item.city != normalized:
            return 50.0

        return 0.0
