"""Expense assistant manifest and tool/subagent registry helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml

AgentRole = Literal["expense_assistant", "manager_explain", "manager"]

def _load_expense_agent_manifest() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "agents" / "expense_assistant.yaml"
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _manifest_list(manifest: dict, *keys: str) -> list[str]:
    value: Any = manifest
    for key in keys:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _manifest_subagents(manifest: dict) -> dict[str, dict]:
    raw = manifest.get("subagents") or {}
    return raw if isinstance(raw, dict) else {}


def _subagent_allowed_tools_from_manifest(manifest: dict) -> dict[str, list[str]]:
    return {
        str(name): _manifest_list(spec, "allowed_tools")
        for name, spec in _manifest_subagents(manifest).items()
        if isinstance(spec, dict)
    }


def _subagent_tool_map_from_allowed_tools(allowed_tools: dict[str, list[str]]) -> dict[str, str]:
    tool_map: dict[str, str] = {}
    for subagent, tools in allowed_tools.items():
        for tool_name in tools:
            tool_map[tool_name] = subagent
    return tool_map


def _subagent_attr_from_manifest(manifest: dict, attr: str) -> dict[str, list[str]]:
    return {
        str(name): _manifest_list(spec, attr)
        for name, spec in _manifest_subagents(manifest).items()
        if isinstance(spec, dict)
    }


_EXPENSE_AGENT_MANIFEST = _load_expense_agent_manifest()


TOOL_REGISTRY: dict[str, list[str]] = {
    # ── expense_assistant ─────────────────────────────────────────────
    # One employee-facing assistant for the drawer. It can answer policy /
    # history / budget questions everywhere, and can write draft fields only
    # when run_agent is bound to an owned draft_id. That keeps the product as
    # one assistant while preserving tool-level safety boundaries.
    "expense_assistant": _manifest_list(_EXPENSE_AGENT_MANIFEST, "runtime", "tool_order"),
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

TOOL_REGISTRY_READ: dict[str, list[str]] = {
    "expense_assistant": _manifest_list(_EXPENSE_AGENT_MANIFEST, "runtime", "read_tools"),
}

TOOL_REGISTRY_WRITE: dict[str, list[str]] = {
    "expense_assistant": _manifest_list(_EXPENSE_AGENT_MANIFEST, "runtime", "write_tools"),
}


def _canonical_agent_role(role: str) -> str:
    """Map retired employee chat modes onto the unified assistant."""
    if role == "employee":
        return "expense_assistant"
    return role

RECEIPT_ANALYSER_SKILLS: dict[str, str] = {
    str(name): str(description)
    for name, description in (_EXPENSE_AGENT_MANIFEST.get("skills") or {}).items()
}

SUBAGENT_ALLOWED_TOOLS: dict[str, list[str]] = _subagent_allowed_tools_from_manifest(
    _EXPENSE_AGENT_MANIFEST
)
SUBAGENT_TOOL_MAP: dict[str, str] = _subagent_tool_map_from_allowed_tools(SUBAGENT_ALLOWED_TOOLS)
SUBAGENT_SKILLS: dict[str, list[str]] = _subagent_attr_from_manifest(
    _EXPENSE_AGENT_MANIFEST,
    "skills",
)
SUBAGENT_CONNECTORS: dict[str, list[str]] = _subagent_attr_from_manifest(
    _EXPENSE_AGENT_MANIFEST,
    "connectors",
)
EXTERNAL_EVIDENCE_TOOLS = set(
    _manifest_list(_EXPENSE_AGENT_MANIFEST, "runtime", "external_evidence_tools")
)
MAX_EXTERNAL_EVIDENCE_TOOL_CALLS = int(
    (_EXPENSE_AGENT_MANIFEST.get("runtime") or {}).get("max_external_evidence_tool_calls", 5)
)


def _subagent_for_tool(tool_name: str) -> str:
    return SUBAGENT_TOOL_MAP.get(tool_name, "receipt-analysis-orchestrator")
