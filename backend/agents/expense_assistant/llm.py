"""LLM adapters for the expense assistant runtime."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date
from typing import Any, Optional

from backend.services.config_loader import load_prompt
from backend.services.injection_guard import scan_text

from .manifest import _canonical_agent_role
from .prompts import _SYSTEM_PROMPTS, _format_steering_block

def _clean_llm_config_value(name: str, raw: Optional[str], default: str = "") -> str:
    """Return a header-safe value for LLM SDK config.

    API keys and model ids travel through HTTP headers/URLs in OpenAI-compatible
    clients. If a shell loader accidentally includes inline Chinese comments
    from .env, httpx fails with a low-level UnicodeEncodeError. Strip common
    inline comments and raise a clear config error if non-ASCII remains.
    """
    raw = raw if raw is not None else default
    value = raw.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    if value and value[0] in {"'", '"'} and value[-1:] == value[0]:
        value = value[1:-1].strip()

    bad = next((ch for ch in value if ord(ch) > 127), None)
    if bad:
        raise RuntimeError(
            f"{name} contains non-ASCII character U+{ord(bad):04X}. "
            "请删除 .env 里的中文注释/占位文字；该变量只能包含纯 ASCII 的 key 或 model id。"
        )
    return value


def _clean_llm_env_value(name: str, default: str = "") -> str:
    return _clean_llm_config_value(name, os.getenv(name), default)


_IDENTITY_QUESTION_RE = re.compile(
    r"(什么模型|哪个模型|底层模型|你是谁|你是什么|你是\s*(claude|chatgpt|gpt|deepseek)|"
    r"what\s+model|which\s+model|who\s+are\s+you|are\s+you\s+(claude|chatgpt|gpt|deepseek))",
    re.IGNORECASE,
)


def _is_identity_question(text: str) -> bool:
    return bool(_IDENTITY_QUESTION_RE.search(text or ""))


def _configured_model_label() -> str:
    """Return the active runtime model label without exposing secrets."""
    if _clean_llm_env_value("AGENT_USE_REAL_LLM") == "1":
        if _clean_llm_env_value("DEEPSEEK_API_KEY"):
            model = _clean_llm_env_value("DEEPSEEK_MODEL", "deepseek-v4-pro") or "deepseek-v4-pro"
            return f"DeepSeek ({model})"
        if _clean_llm_env_value("OPENAI_API_KEY"):
            model = _clean_llm_env_value("OPENAI_MODEL", "gpt-4o") or "gpt-4o"
            return f"OpenAI ({model})"
    return "MockLLM 本地规则模型"


def _identity_answer() -> str:
    model_label = _configured_model_label()
    return (
        "我是 ExpenseFlow 的 AI 报销助手，不是 Claude。"
        f"当前后端运行模型是 {model_label}；具体调用记录可以在 eval trace 里查看。"
    )



class LLMResponse:
    """LLM 一轮响应的抽象 — 对应 Anthropic API 的 Message 结构。"""
    def __init__(
        self,
        text: str = "",
        tool_calls: Optional[list[dict]] = None,
        stop_reason: str = "end_turn",
        reasoning_content: str = "",
    ):
        self.text = text
        self.tool_calls = tool_calls or []  # [{id, name, input}, ...]
        self.stop_reason = stop_reason      # "end_turn" | "tool_use"
        self.reasoning_content = reasoning_content


class BaseLLM:
    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "expense_assistant",
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
        agent_role: str = "expense_assistant",
    ) -> LLMResponse:
        agent_role = _canonical_agent_role(agent_role)
        if agent_role == "manager":
            return self._manager_turn(messages)

        # Unified expense assistant. In stateless drawer mode it behaves like
        # policy/history/budget QA. When run_agent is bound to a draft_id, the
        # same assistant can also write draft fields and complete missing
        # receipt cases through external evidence.
        last_user_idx = self._find_last(messages, role="user", text_not_tool=True)
        last_user_text = ""
        if last_user_idx is not None:
            last_user_text = self._extract_text(messages[last_user_idx]).lower()
        has_draft_context = self._has_draft_context(messages)

        if self._is_document_prompt_injection_request(last_user_text):
            if scan_text(last_user_text):
                return LLMResponse(
                    text=(
                        "我检测到发票/文档里包含类似 prompt injection 的指令。"
                        "这些内容只会作为风险信号记录，不会被当作系统指令执行；"
                        "我也不会因此提交、批准或篡改金额。请提供真实消费信息或重新上传干净票据。"
                    ),
                    stop_reason="end_turn",
                )

        if self._is_forbidden_action_request(last_user_text):
            return LLMResponse(
                text="我不能提交、批准、拒绝或付款。请你在界面里手动点击对应按钮，审批也必须由有权限的人完成。",
                stop_reason="end_turn",
            )

        if self._has_qa_tool_result(messages, after_idx=last_user_idx):
            return self._qa_turn(messages)

        if not has_draft_context or self._is_readonly_qa_request(last_user_text):
            if not self._is_draft_completion_request(last_user_text):
                return self._qa_turn(messages)

        extract_result = self._find_tool_result(messages, "extract_receipt_fields")
        dup_result     = self._find_tool_result(messages, "check_duplicate_invoice")
        suggest_result = self._find_tool_result(messages, "suggest_category")
        didi_result    = self._find_tool_result(messages, "lookup_didi_trip")
        ctrip_result   = self._find_tool_result(messages, "lookup_ctrip_booking")
        card_result    = self._find_tool_result(messages, "lookup_card_transaction")

        field_update = self._extract_draft_field_update(last_user_text)
        if field_update:
            if not has_draft_context:
                return LLMResponse(
                    text="我需要先绑定一个报销草稿才能修改字段。请在快速报销页打开或创建草稿后再告诉我要改什么。",
                    stop_reason="end_turn",
                )
            return LLMResponse(
                text="好的，我把当前草稿字段改掉。",
                tool_calls=[self._tool_call("update_draft_field", field_update)],
                stop_reason="tool_use",
            )

        if not has_draft_context and self._is_draft_completion_request(last_user_text):
            return LLMResponse(
                text="我可以补齐草稿字段，但需要先绑定一个当前报销草稿。请在快速报销页打开草稿或用右侧快捷按钮创建无发票草稿。",
                stop_reason="end_turn",
            )

        # 滴滴/打车发票丢失或用户给了滴滴订单号：走外部证据补齐路径。
        # 这个分支必须放在 OCR 之前，否则 MockLLM 会把任何用户消息都先送去识别发票。
        if self._is_didi_completion_request(last_user_text):
            if didi_result is None:
                args = self._extract_didi_lookup_args(last_user_text)
                return LLMResponse(
                    text="我先查一下滴滴行程证据，用于补齐这笔交通费。",
                    tool_calls=[self._tool_call("lookup_didi_trip", args)],
                    stop_reason="tool_use",
                )

            if didi_result.get("error"):
                return LLMResponse(
                    text=(
                        "我尝试查询滴滴行程，但当前外部证据接口返回错误。"
                        "请检查滴滴 MCP sandbox 配置，或补充订单号、日期、金额后再试。"
                    ),
                    stop_reason="end_turn",
                )

            candidates = didi_result.get("candidates") or []
            if len(candidates) != 1:
                return LLMResponse(
                    text=(
                        "我查到了多个或没有明确匹配的滴滴行程，暂时不会自动写入表单。"
                        "请补充订单号、日期、金额或上下车地点，我再帮你确认。"
                    ),
                    stop_reason="end_turn",
                )
            trip = candidates[0] or {}

            if card_result is None:
                card_args = {
                    "merchant_hint": "DIDI",
                }
                amount = self._net_amount(trip)
                if amount is not None:
                    card_args["amount"] = amount
                if trip.get("date"):
                    card_args["date"] = str(trip["date"])
                return LLMResponse(
                    text="滴滴行程已找到，我再核对信用卡/公司卡扣款后决定是否写入。",
                    tool_calls=[self._tool_call("lookup_card_transaction", card_args)],
                    stop_reason="tool_use",
                )

            card_candidates = card_result.get("candidates") or []
            if card_result.get("error") or len(card_candidates) != 1:
                return LLMResponse(
                    text="我找到了滴滴行程，但还没有唯一匹配的信用卡/公司卡扣款。请补充扣款日期、金额或卡交易线索。",
                    stop_reason="end_turn",
                )
            trip_amount = self._net_amount(trip)
            card_amount = self._net_amount(card_candidates[0] or {})
            if trip_amount is not None and card_amount is not None and abs(trip_amount - card_amount) > 0.01:
                return LLMResponse(
                    text=f"滴滴金额 {trip_amount:g} 元与信用卡扣款 {card_amount:g} 元不一致，我先不写入。请确认实际金额。",
                    stop_reason="end_turn",
                )
            trip_date = str(trip.get("date") or "")
            card_date = str((card_candidates[0] or {}).get("date") or "")
            if trip_date and card_date and trip_date != card_date:
                return LLMResponse(
                    text=f"滴滴日期 {trip_date} 与信用卡扣款日期 {card_date} 不一致，我先不写入。请确认正确消费日期。",
                    stop_reason="end_turn",
                )

            if self._has_draft_writes(messages):
                return LLMResponse(
                    text="已根据滴滴行程和信用卡扣款补齐草稿。请检查金额、日期和路线说明，确认无误后再手动提交。",
                    stop_reason="end_turn",
                )

            if not self._has_draft_writes(messages):
                tc = self._didi_draft_writes(didi_result, card_result)
                if tc:
                    return LLMResponse(
                        text="滴滴行程和信用卡扣款一致，我把可核验字段写入草稿。",
                        tool_calls=tc,
                        stop_reason="tool_use",
                    )

                return LLMResponse(
                    text=(
                        "滴滴 MCP 已返回行程信息，但没有足够的金额、日期或路线字段可自动写入。"
                        "请补充扣款金额或日期后再试。"
                    ),
                    stop_reason="end_turn",
                )

        if self._is_ctrip_completion_request(last_user_text):
            if ctrip_result is None:
                args = self._extract_ctrip_lookup_args(last_user_text)
                return LLMResponse(
                    text="我先查一下携程订单证据，包括订单状态、退款和发票状态。",
                    tool_calls=[self._tool_call("lookup_ctrip_booking", args)],
                    stop_reason="tool_use",
                )

            candidates = ctrip_result.get("candidates") or []
            if len(candidates) != 1:
                return LLMResponse(
                    text=(
                        "我没有查到唯一匹配的携程订单，暂时不会写草稿。"
                        "请补充订单号、日期、金额、城市或酒店/航班信息。"
                    ),
                    stop_reason="end_turn",
                )

            booking = candidates[0] or {}
            if card_result is None:
                return LLMResponse(
                    text="我再查一下公司卡/信用卡扣款，用来和携程订单交叉核验。",
                    tool_calls=[self._tool_call("lookup_card_transaction", self._card_args_from_booking(booking))],
                    stop_reason="tool_use",
                )

            decision = self._ctrip_reconciliation_decision(booking, card_result, last_user_text)
            if decision["status"] == "blocked_write":
                return LLMResponse(
                    text=f"我不会自动写入草稿：{decision['reason']}。请补充说明或上传可验证证据后再继续。",
                    stop_reason="end_turn",
                )

            if self._has_draft_writes(messages):
                labels = "、".join(decision.get("labels") or [])
                return LLMResponse(
                    text=(
                        f"✅ 已根据携程订单和信用卡扣款补齐草稿（{labels or 'evidence_matched'}）。"
                        "请检查字段来源和说明，确认无误后手动提交。"
                    ),
                    stop_reason="end_turn",
                )

            writes = self._ctrip_draft_writes(booking, card_result, decision)
            return LLMResponse(
                text=f"{decision['reason']} 我会把可核验字段写入草稿，并保留证据来源。",
                tool_calls=writes,
                stop_reason="tool_use",
            )

        if self._is_external_evidence_request(last_user_text):
            return LLMResponse(
                text=(
                    "可以。请告诉我是滴滴、携程还是信用卡证据，并尽量提供订单号、日期、金额、城市或路线。"
                    "我只会在证据唯一且字段明确时写入草稿。"
                ),
                stop_reason="end_turn",
            )

        # 第 1 步：只有在草稿确实有发票、或用户明确要求识别发票时才触发 OCR。
        if extract_result is None and last_user_idx is not None and self._should_extract_receipt(messages, last_user_text):
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
        """只读 QA 模式的规则脚本（MockLLM 下 unified assistant 走这条）。

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

        explicit_submission_id = self._extract_named_id(text, ("submission_id", "id"))
        explicit_employee_id = self._extract_named_id(text, ("employee_id",))

        why_kws     = ("为什么", "why", "原因", "怎么", "高风险", "风险", "解释", "分析")
        history_kws = ("历史", "消费模式", "记录", "习惯", "history", "pattern")
        queue_kws   = ("待审", "队列", "等我", "等审", "需要我", "queue", "pending", "approval")
        team_kws    = ("团队", "部门", "team", "本月花", "本季度", "department", "支出")

        if any(k in text for k in why_kws) and (explicit_submission_id or ctx_line_id):
            submission_id = explicit_submission_id or ctx_line_id
            return LLMResponse(
                text="我去拉一下这张单的审计报告…",
                tool_calls=[self._tool_call(
                    "get_submission_for_review", {"submission_id": submission_id},
                )],
                stop_reason="tool_use",
            )
        if explicit_employee_id and any(k in text for k in history_kws):
            return LLMResponse(
                text="我查一下这位员工的历史报销记录…",
                tool_calls=[self._tool_call(
                    "get_employee_submission_history",
                    {"employee_id": explicit_employee_id},
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
    def _extract_named_id(text: str, names: tuple[str, ...]) -> Optional[str]:
        for name in names:
            match = re.search(rf"\b{name}\s*=\s*([a-z0-9][a-z0-9_-]*)", text)
            if match:
                return match.group(1)
        return None

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

    def _has_draft_context(self, messages: list[dict]) -> bool:
        return any("[当前草稿上下文]" in self._extract_text(m) for m in messages)

    def _draft_has_receipt(self, messages: list[dict]) -> bool:
        for msg in messages:
            text = self._extract_text(msg)
            if "[当前草稿上下文]" in text and "receipt_uploaded=true" in text:
                return True
        return False

    def _has_qa_tool_result(self, messages: list[dict], after_idx: Optional[int] = None) -> bool:
        qa_tools = {
            "get_spend_summary",
            "get_report_detail",
            "get_my_recent_submissions",
            "get_policy_rules",
        }
        tool_names_by_id: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content", [])
            if msg.get("role") != "assistant" or not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_names_by_id[str(block.get("id"))] = str(block.get("name"))

        start = (after_idx + 1) if after_idx is not None else 0
        for msg in messages[start:]:
            content = msg.get("content", [])
            if msg.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if tool_names_by_id.get(str(block.get("tool_use_id"))) in qa_tools:
                        return True
        return False

    @staticmethod
    def _is_forbidden_action_request(text: str) -> bool:
        if not text:
            return False
        action = any(k in text for k in ("提交", "批准", "审批通过", "拒绝", "付款", "打款", "approve", "submit", "pay"))
        override = any(k in text for k in ("忽略", "绕过", "直接", "不用确认", "ignore", "bypass"))
        return action and override

    @staticmethod
    def _is_document_prompt_injection_request(text: str) -> bool:
        if not text:
            return False
        doc_context = any(k in text for k in ("发票", "票据", "pdf", "截图", "图片", "文档", "receipt", "invoice"))
        injection = any(k in text for k in (
            "忽略", "绕过", "直接提交", "直接批准", "改金额", "调用工具",
            "ignore", "bypass", "override", "submit", "approve", "update_draft_field",
        ))
        return doc_context and injection

    @staticmethod
    def _is_readonly_qa_request(text: str) -> bool:
        if not text:
            return False
        kws = (
            "政策", "规定", "限额", "标准", "超标", "policy", "能报", "可以报", "允许",
            "我这个月", "本月", "本季度", "花了多少", "消费汇总", "预算", "历史",
            "最近", "状态", "报销记录",
        )
        return any(k in text for k in kws)

    @staticmethod
    def _is_draft_completion_request(text: str) -> bool:
        if not text:
            return False
        explicit_completion = any(k in text for k in ("补齐", "补全", "外部证据", "帮我填", "填写"))
        missing_receipt = any(k in text for k in ("无发票", "没有发票", "发票找不到", "发票丢"))
        provider_or_edit = any(k in text for k in (
            "滴滴", "didi", "携程", "ctrip", "信用卡", "修改", "改成", "字段",
        ))
        return provider_or_edit or (explicit_completion and missing_receipt)

    @staticmethod
    def _is_external_evidence_request(text: str) -> bool:
        if not text:
            return False
        evidence = any(k in text for k in ("无发票", "没有发票", "发票找不到", "发票丢", "外部证据"))
        completion_intent = any(k in text for k in ("补齐", "补全", "帮我填", "填写", "外部证据"))
        other_provider = any(k in text for k in ("携程", "ctrip", "信用卡"))
        didi_specific = any(k in text for k in ("滴滴", "didi", "打车", "出租车", "网约车"))
        return (other_provider or (evidence and completion_intent)) and not didi_specific

    @staticmethod
    def _is_ctrip_completion_request(text: str) -> bool:
        if not text:
            return False
        provider = any(k in text for k in ("携程", "ctrip", "trip.com", "booking"))
        travel = any(k in text for k in ("酒店", "机票", "航班", "住宿", "hotel", "flight", "订单"))
        lifecycle = any(k in text for k in ("取消", "退款", "改签", "重订", "rebook", "cancel", "refund", "发票状态"))
        completion = any(k in text for k in ("补齐", "补全", "发票", "模糊", "找不到", "无发票", "报销", "类别"))
        return provider or (travel and (lifecycle or completion))

    def _should_extract_receipt(self, messages: list[dict], text: str) -> bool:
        if self._is_external_evidence_request(text) or self._is_didi_completion_request(text):
            return False
        receipt_intent = any(k in text for k in ("识别", "ocr", "发票", "票据", "上传", "图片", "pdf"))
        return self._draft_has_receipt(messages) or receipt_intent

    def _extract_draft_field_update(self, text: str) -> Optional[dict]:
        if not text or not any(k in text for k in ("改", "修改", "换成", "设为", "填成")):
            return None

        amount = re.search(r"(?:金额|总额|钱|amount)[^\d]*(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
        if amount:
            return {"field": "amount", "value": amount.group(1), "source": "user_confirmed"}

        category_map = {
            "餐饮": "meal", "吃饭": "meal", "餐费": "meal",
            "交通": "transport", "打车": "transport", "车费": "transport", "机票": "transport",
            "住宿": "accommodation", "酒店": "accommodation",
            "招待": "entertainment", "团建": "entertainment", "娱乐": "entertainment",
            "其他": "other",
        }
        if any(k in text for k in ("类别", "分类", "类型", "category")):
            for label, value in category_map.items():
                if label in text:
                    return {"field": "category", "value": value, "source": "user_confirmed"}

        date_match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        if date_match and any(k in text for k in ("日期", "时间", "date")):
            y, m, d = (int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            return {"field": "date", "value": f"{y:04d}-{m:02d}-{d:02d}", "source": "user_confirmed"}

        merchant = re.search(r"(?:商户|商家|merchant)[：:\s]*(.+)$", text, flags=re.IGNORECASE)
        if merchant:
            return {"field": "merchant", "value": merchant.group(1).strip(), "source": "user_confirmed"}

        return None

    @staticmethod
    def _is_didi_completion_request(text: str) -> bool:
        if not text:
            return False
        has_didi = any(k in text for k in ("滴滴", "didi", "打车", "出租车", "网约车"))
        has_completion_intent = any(k in text for k in (
            "订单", "order", "trip", "行程", "发票丢", "发票找不到",
            "没有发票", "补齐", "补全", "报销",
        ))
        return has_didi and has_completion_intent

    @staticmethod
    def _extract_didi_lookup_args(text: str) -> dict:
        args: dict = {}
        order_match = re.search(
            r"(?:订单号|订单|order[_\s-]?id|trip[_\s-]?id)[:：#\s]*([a-z0-9][a-z0-9_-]{2,})",
            text,
            flags=re.IGNORECASE,
        )
        if order_match:
            args["order_id"] = order_match.group(1).strip()

        date_match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        if date_match:
            y, m, d = (int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            args["date"] = f"{y:04d}-{m:02d}-{d:02d}"
        else:
            md_match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
            if md_match:
                args["date"] = f"{date.today().year:04d}-{int(md_match.group(1)):02d}-{int(md_match.group(2)):02d}"

        amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|cny|￥|¥)", text, flags=re.IGNORECASE)
        if amount_match:
            args["amount"] = float(amount_match.group(1))

        for city in ("北京", "上海", "深圳", "广州", "杭州", "成都", "南京", "重庆", "西安", "苏州", "长沙", "郑州"):
            if city in text:
                args["city"] = city
                break
        return args

    @staticmethod
    def _extract_ctrip_lookup_args(text: str) -> dict:
        args: dict = {}
        booking_match = re.search(
            r"(?:订单号|订单|booking[_\s-]?id|order[_\s-]?id)[:：#\s]*([a-z0-9][a-z0-9_-]{2,})",
            text,
            flags=re.IGNORECASE,
        )
        if booking_match:
            args["booking_id"] = booking_match.group(1).strip()

        if any(k in text for k in ("酒店", "住宿", "hotel")):
            args["booking_type"] = "hotel"
        elif any(k in text for k in ("机票", "航班", "flight", "飞机")):
            args["booking_type"] = "flight"
        else:
            args["booking_type"] = "unknown"

        date_match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        if date_match:
            y, m, d = (int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            args["date"] = f"{y:04d}-{m:02d}-{d:02d}"
        else:
            md_match = re.search(r"(\d{1,2})月(\d{1,2})日", text)
            if md_match:
                args["date"] = f"{date.today().year:04d}-{int(md_match.group(1)):02d}-{int(md_match.group(2)):02d}"

        amount = MockLLM._extract_amount_from_text(text)
        if amount is not None:
            args["amount"] = amount

        for city in ("北京", "上海", "深圳", "广州", "杭州", "成都", "南京", "重庆", "西安", "苏州", "长沙", "郑州"):
            if city in text:
                args["merchant_hint"] = city
                break
        return args

    @staticmethod
    def _extract_amount_from_text(text: str) -> Optional[float]:
        amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|rmb|cny|￥|¥)", text, flags=re.IGNORECASE)
        if not amount_match:
            return None
        try:
            return float(amount_match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _card_args_from_booking(booking: dict) -> dict:
        args: dict = {}
        if booking.get("net_amount", booking.get("amount")) is not None:
            args["amount"] = float(booking.get("net_amount", booking.get("amount")))
        if booking.get("date"):
            args["date"] = str(booking["date"])
        if booking.get("booking_type") == "hotel":
            args["merchant_hint"] = "HOTEL"
        elif booking.get("booking_type") == "flight":
            args["merchant_hint"] = "CTRIP"
        elif booking.get("vendor"):
            args["merchant_hint"] = str(booking["vendor"])
        return args

    @staticmethod
    def _net_amount(item: dict) -> Optional[float]:
        raw = item.get("net_amount", item.get("amount"))
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _ctrip_reconciliation_decision(self, booking: dict, card_result: dict, text: str) -> dict:
        claim_amount = self._extract_amount_from_text(text)
        booking_net = self._net_amount(booking)
        card_candidates = card_result.get("candidates") or [] if isinstance(card_result, dict) else []
        card_net = self._net_amount(card_candidates[0]) if len(card_candidates) == 1 else None
        verified_amount = booking_net
        if card_net is not None and booking_net is not None:
            verified_amount = min(booking_net, card_net)

        status = str(booking.get("status") or "").lower()
        refund_amount = float(booking.get("refund_amount") or 0)
        labels: list[str] = []

        if status in {"cancelled", "canceled"} and (booking_net is None or booking_net <= 0):
            return {
                "status": "blocked_write",
                "labels": ["booking_cancelled", "full_refund"],
                "claim_amount": claim_amount,
                "verified_amount": verified_amount,
                "needs_user_clarification": True,
                "reason": "携程订单已取消且净额为 0，不能按原订单自动报销",
            }
        if booking_net is not None and booking_net <= 0:
            return {
                "status": "blocked_write",
                "labels": ["full_refund"],
                "claim_amount": claim_amount,
                "verified_amount": verified_amount,
                "needs_user_clarification": True,
                "reason": "外部证据显示已全额退款，缺少可报销净额",
            }
        if claim_amount is not None and verified_amount is not None and claim_amount > verified_amount + 1:
            return {
                "status": "blocked_write",
                "labels": ["over_claim_risk"],
                "claim_amount": claim_amount,
                "verified_amount": verified_amount,
                "needs_user_clarification": True,
                "reason": f"报销金额 {claim_amount:g} 高于携程/信用卡可验证净额 {verified_amount:g}",
            }

        if claim_amount is not None and verified_amount is not None and claim_amount < verified_amount - 1:
            labels.append("partial_claim")
        if status == "rebooked":
            labels.append("rebooked_current_booking")
        if refund_amount > 0:
            labels.append("partial_refund")
        if len(card_candidates) == 1:
            labels.append("ctrip_card_match")
        else:
            labels.append("ctrip_booking_only")
        if booking.get("invoice_status") == "pending":
            labels.append("invoice_pending")
        if not labels:
            labels.append("evidence_matched")

        final_claim = claim_amount if claim_amount is not None else verified_amount
        category = "accommodation" if booking.get("booking_type") == "hotel" else "transport"
        return {
            "status": "can_write_draft",
            "labels": labels,
            "claim_amount": final_claim,
            "verified_amount": verified_amount,
            "category": category,
            "needs_user_clarification": "partial_claim" in labels,
            "reason": (
                "携程订单与信用卡扣款可交叉核验"
                if len(card_candidates) == 1
                else "携程订单唯一匹配，但信用卡没有唯一匹配"
            ),
        }

    def _ctrip_draft_writes(self, booking: dict, card_result: dict, decision: dict) -> list[dict]:
        source = "ctrip_card_match" if "ctrip_card_match" in decision.get("labels", []) else "ctrip_booking"
        if "partial_claim" in decision.get("labels", []):
            source = "ctrip_booking_partial_claim"
        merchant = (
            booking.get("vendor")
            if booking.get("booking_type") == "hotel"
            else booking.get("merchant")
        ) or "携程旅行"
        amount = decision.get("claim_amount", booking.get("net_amount", booking.get("amount")))
        category = decision.get("category") or ("accommodation" if booking.get("booking_type") == "hotel" else "transport")
        evidence = [
            f"携程订单：{booking.get('booking_id') or '-'}",
            f"状态：{booking.get('status') or 'active'}",
            f"发票状态：{booking.get('invoice_status') or '-'}",
            f"原金额：{booking.get('amount')}",
            f"退款：{booking.get('refund_amount', 0)}",
            f"净额：{booking.get('net_amount', booking.get('amount'))}",
        ]
        if booking.get("route"):
            evidence.append(f"行程：{booking['route']}")
        if booking.get("hotel"):
            evidence.append(f"酒店：{booking['hotel']}")
        if decision.get("labels"):
            evidence.append("标签：" + ",".join(decision["labels"]))

        writes = [
            self._tool_call("update_draft_field", {
                "field": "merchant", "value": str(merchant), "source": source,
            }),
            self._tool_call("update_draft_field", {
                "field": "amount", "value": str(amount or 0), "source": source,
            }),
            self._tool_call("update_draft_field", {
                "field": "date", "value": str(booking.get("date") or ""), "source": source,
            }),
            self._tool_call("update_draft_field", {
                "field": "category", "value": category, "source": source,
            }),
            self._tool_call("update_draft_field", {
                "field": "description", "value": "；".join(evidence), "source": source,
            }),
        ]
        return writes

    def _didi_draft_writes(self, didi_result: dict, card_result: Optional[dict] = None) -> list[dict]:
        candidates = didi_result.get("candidates") or []
        if len(candidates) != 1:
            return []
        trip = candidates[0] or {}
        card_candidates = (card_result or {}).get("candidates") or []
        source = "didi_card_match" if len(card_candidates) == 1 else (didi_result.get("source") or "didi_lookup")
        tc: list[dict] = [
            self._tool_call("update_draft_field", {
                "field": "merchant",
                "value": str(trip.get("merchant") or "滴滴出行"),
                "source": source,
            }),
            self._tool_call("update_draft_field", {
                "field": "category",
                "value": "transport",
                "source": "agent_suggested",
            }),
        ]
        if trip.get("amount") is not None:
            tc.append(self._tool_call("update_draft_field", {
                "field": "amount",
                "value": str(trip["amount"]),
                "source": source,
            }))
        if trip.get("date"):
            tc.append(self._tool_call("update_draft_field", {
                "field": "date",
                "value": str(trip["date"]),
                "source": source,
            }))

        route = " -> ".join(str(v) for v in (trip.get("from"), trip.get("to")) if v)
        evidence_bits = [f"来源：{source}"]
        if trip.get("trip_id"):
            evidence_bits.append(f"订单号：{trip['trip_id']}")
        if route:
            evidence_bits.append(f"路线：{route}")
        if trip.get("amount") is not None:
            evidence_bits.append(f"金额：{trip['amount']} {trip.get('currency') or 'CNY'}")
        if trip.get("status"):
            evidence_bits.append(f"状态：{trip['status']}")
        if trip.get("mcp_summary"):
            evidence_bits.append(f"MCP摘要：{trip['mcp_summary']}")

        tc.append(self._tool_call("update_draft_field", {
            "field": "description",
            "value": "滴滴行程证据；" + "；".join(evidence_bits),
            "source": source,
        }))
        return tc

class RealLLM(BaseLLM):
    """OpenAI-compatible API 调用（GPT-4o / DeepSeek / 其他兼容服务）。

    设置 OPENAI_API_KEY + AGENT_USE_REAL_LLM=1 启用 GPT-4o。
    设置 DEEPSEEK_API_KEY + AGENT_USE_REAL_LLM=1 启用 DeepSeek。

    消息格式转换：内部使用 Anthropic 格式（tool_use / tool_result blocks），
    发送前翻译成 OpenAI format（tool_calls / role=tool），
    返回后再翻译回 LLMResponse。外层 run_agent loop 无需任何修改。
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
            kwargs: dict = {}
            kwargs["api_key"] = (
                _clean_llm_config_value("api_key", api_key)
                if api_key is not None
                else _clean_llm_env_value("OPENAI_API_KEY")
            )
            if base_url:
                kwargs["base_url"] = base_url
            self._client = AsyncOpenAI(**kwargs)
            self._model = (
                _clean_llm_config_value("model", model)
                if model is not None
                else _clean_llm_env_value("OPENAI_MODEL", "gpt-4o")
            ) or "gpt-4o"
            self._roundtrip_reasoning_content = "deepseek" in str(base_url or "").lower()
        except Exception as exc:
            raise RuntimeError(f"OpenAI SDK 初始化失败：{exc}") from exc

    async def next_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        agent_role: str = "expense_assistant",
    ) -> LLMResponse:
        from backend.services.trace import record_trace, TraceTimer

        # Prefer the dashboard-edited prompt (eval_prompts.json), fall back to
        # the hardcoded default if the JSON entry is missing or empty. Loaded
        # per-request so dashboard edits flow in without a server restart.
        base_prompt = (
            load_prompt(f"chat_{agent_role}")
            or _SYSTEM_PROMPTS.get(agent_role, _SYSTEM_PROMPTS["expense_assistant"])
        )
        system = (
            f"当前日期：{date.today().isoformat()}。用户提到的日期如果没有年份，默认为当前年份。\n\n"
            + base_prompt
            + _format_steering_block(agent_role)
        )
        oai_messages = self._to_oai_messages(
            messages,
            system,
            include_reasoning_content=self._roundtrip_reasoning_content,
        )
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
        reasoning_content = ""
        usage: Optional[dict] = None
        err: Optional[str] = None
        timer = TraceTimer()
        try:
            with timer:
                response = await self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message
            text = msg.content or ""
            reasoning_content = self._extract_reasoning_content(msg)
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
                prompt=self._redact_reasoning_for_trace(oai_messages),
                response=text or None,
                parsed_output={
                    "tool_calls": tool_calls,
                    "stop_reason": stop_reason,
                    "reasoning_content_present": bool(reasoning_content),
                } if not err else None,
                latency_ms=timer.elapsed_ms or None,
                token_usage=usage,
                error=err,
            )

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            reasoning_content=reasoning_content,
        )

    # ── Format translators ─────────────────────────────────────────

    @staticmethod
    def _to_oai_messages(
        messages: list[dict],
        system: str,
        *,
        include_reasoning_content: bool = False,
    ) -> list[dict]:
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
                reasoning_blocks = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "reasoning_content"
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
                if include_reasoning_content and reasoning_blocks:
                    # DeepSeek V4 thinking mode requires reasoning_content to
                    # be round-tripped on assistant turns that produce tools.
                    oai_msg["reasoning_content"] = "\n".join(reasoning_blocks)
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

    @staticmethod
    def _extract_reasoning_content(message: Any) -> str:
        value = getattr(message, "reasoning_content", None)
        if value:
            return str(value)
        extra = getattr(message, "model_extra", None)
        if isinstance(extra, dict) and extra.get("reasoning_content"):
            return str(extra["reasoning_content"])
        if hasattr(message, "model_dump"):
            try:
                dumped = message.model_dump()
                if dumped.get("reasoning_content"):
                    return str(dumped["reasoning_content"])
            except Exception:
                return ""
        return ""

    @staticmethod
    def _redact_reasoning_for_trace(messages: list[dict]) -> list[dict]:
        redacted: list[dict] = []
        for msg in messages:
            copy_msg = dict(msg)
            if copy_msg.get("reasoning_content"):
                copy_msg["reasoning_content"] = "[redacted]"
            redacted.append(copy_msg)
        return redacted


def get_llm() -> BaseLLM:
    """根据环境切换 LLM backend。

    MockLLM（默认）：无需任何 API Key，规则脚本，支持全套 eval。
    RealLLM（GPT-4o）：设置 OPENAI_API_KEY + AGENT_USE_REAL_LLM=1。
    DeepSeek：设置 DEEPSEEK_API_KEY + AGENT_USE_REAL_LLM=1。
    """
    if _clean_llm_env_value("AGENT_USE_REAL_LLM") == "1":
        deepseek_key = _clean_llm_env_value("DEEPSEEK_API_KEY")
        if deepseek_key:
            return RealLLM(
                model=_clean_llm_env_value("DEEPSEEK_MODEL", "deepseek-v4-pro") or "deepseek-v4-pro",
                api_key=deepseek_key,
                base_url="https://api.deepseek.com",
            )
        openai_key = _clean_llm_env_value("OPENAI_API_KEY")
        if openai_key:
            return RealLLM(api_key=openai_key)
    return MockLLM()
