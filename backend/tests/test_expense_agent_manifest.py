"""Expense assistant manifest tests.

These checks keep the 3-subagent contract, runtime tool registries, and trace
metadata aligned. The manifest is the source of truth; chat.py should derive
its runtime maps from it rather than carrying independent copies.
"""
from __future__ import annotations

from backend.api.routes import chat as chat_mod


def test_expense_assistant_tool_registries_are_manifest_derived():
    manifest = chat_mod._EXPENSE_AGENT_MANIFEST

    assert chat_mod.TOOL_REGISTRY["expense_assistant"] == manifest["runtime"]["tool_order"]
    assert chat_mod.TOOL_REGISTRY_READ["expense_assistant"] == manifest["runtime"]["read_tools"]
    assert chat_mod.TOOL_REGISTRY_WRITE["expense_assistant"] == manifest["runtime"]["write_tools"]
    assert chat_mod.EXTERNAL_EVIDENCE_TOOLS == set(manifest["runtime"]["external_evidence_tools"])
    assert chat_mod.MAX_EXTERNAL_EVIDENCE_TOOL_CALLS == manifest["runtime"]["max_external_evidence_tool_calls"]


def test_subagent_tool_skill_connector_maps_are_manifest_derived():
    manifest_subagents = chat_mod._EXPENSE_AGENT_MANIFEST["subagents"]

    for name, spec in manifest_subagents.items():
        assert chat_mod.SUBAGENT_ALLOWED_TOOLS[name] == spec["allowed_tools"]
        assert chat_mod.SUBAGENT_SKILLS[name] == spec["skills"]
        assert chat_mod.SUBAGENT_CONNECTORS[name] == spec["connectors"]
        for tool in spec["allowed_tools"]:
            assert chat_mod.SUBAGENT_TOOL_MAP[tool] == name


def test_subagent_trace_event_exposes_connectors():
    event = chat_mod._subagent_event(
        subagent="evidence-reconciler",
        event="tool_call",
        tool="lookup_ctrip_booking",
        tool_input={"date": "2026-05-09", "amount": 680},
    )

    assert event["allowed_tools"] == chat_mod.SUBAGENT_ALLOWED_TOOLS["evidence-reconciler"]
    assert event["skills"] == chat_mod.SUBAGENT_SKILLS["evidence-reconciler"]
    assert event["connectors"] == chat_mod.SUBAGENT_CONNECTORS["evidence-reconciler"]
    assert "ctrip-evidence-provider" in event["connectors"]
