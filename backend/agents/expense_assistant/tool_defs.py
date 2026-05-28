"""Tool schemas exposed to the expense assistant LLM."""
from __future__ import annotations

from .manifest import TOOL_REGISTRY, _canonical_agent_role

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
                    "description": "字段来源，如 ocr / agent_suggested / user_confirmed / didi_mcp_sandbox / ctrip_card_match",
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
    "lookup_ctrip_booking": {
        "name": "lookup_ctrip_booking",
        "description": "只读查询携程/Trip.com 机票或酒店订单，用于发票缺失、发票模糊或字段不全时补齐商户、日期、金额、行程/入住信息。携程报销补齐必须再调用 lookup_card_transaction 做扣款交叉验证；证据不足时返回候选项，不写草稿。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "用户提供或 OCR 识别的交易/行程日期，YYYY-MM-DD，可选"},
                "amount": {"type": "number", "description": "用户提供或 OCR/信用卡识别的金额，可选"},
                "booking_id": {"type": "string", "description": "携程/Trip.com 订单号，可选"},
                "booking_type": {"type": "string", "enum": ["flight", "hotel", "unknown"], "description": "订单类型，可选"},
                "merchant_hint": {"type": "string", "description": "商户、航司、酒店或城市关键词，可选"},
                "case_id": {"type": "string", "description": "eval 专用：按 case id 选择 mock fixture，可选"},
                "fixture_id": {"type": "string", "description": "eval 专用：直接选择携程 mock fixture，可选"},
            },
            "required": [],
        },
    },
    "lookup_didi_trip": {
        "name": "lookup_didi_trip",
        "description": "只读查询滴滴打车行程，用于打车发票丢失、模糊或字段不全时补齐金额、日期、上下车地点和商户。滴滴报销补齐必须再调用 lookup_card_transaction 做扣款交叉验证；证据不足时返回候选项，不写草稿。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "行程或扣款日期，YYYY-MM-DD，可选"},
                "amount": {"type": "number", "description": "打车金额，可选"},
                "city": {"type": "string", "description": "城市或地点关键词，可选"},
                "order_id": {"type": "string", "description": "滴滴订单 ID，可选。使用 MCP sandbox 的 taxi_query_order 时优先传入。"},
                "case_id": {"type": "string", "description": "eval 专用：按 case id 选择 mock fixture，可选"},
                "fixture_id": {"type": "string", "description": "eval 专用：直接选择滴滴 mock fixture，可选"},
            },
            "required": [],
        },
    },
    "lookup_card_transaction": {
        "name": "lookup_card_transaction",
        "description": "只读查询公司卡/信用卡交易，用于与 OCR、携程、滴滴等来源交叉校验金额、日期和商户。证据不足时返回候选项，不写草稿。",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "扣款日期，YYYY-MM-DD，可选"},
                "amount": {"type": "number", "description": "扣款金额，可选"},
                "merchant_hint": {"type": "string", "description": "商户关键词，可选"},
                "case_id": {"type": "string", "description": "eval 专用：按 case id 选择 mock fixture，可选"},
                "fixture_id": {"type": "string", "description": "eval 专用：直接选择信用卡 mock fixture，可选"},
            },
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


def get_tools_for_role(role: str) -> list[dict]:
    """返回指定 role 允许使用的 tool 定义列表（喂给 LLM 的 tools 参数）。"""
    names = TOOL_REGISTRY.get(_canonical_agent_role(role), [])
    return [_TOOL_DEFS[n] for n in names if n in _TOOL_DEFS]
