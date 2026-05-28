"""Code-based graders for eval cases.

Each grader takes (actual_output, expected_spec) and returns (passed, message).
All graders are deterministic — no LLM calls.
"""
from __future__ import annotations

from typing import Any


def grade_score_range(actual: float, expected: list[float]) -> tuple[bool, str]:
    """Check that actual score is within [lo, hi] range."""
    lo, hi = expected[0], expected[1]
    passed = lo <= actual <= hi
    msg = f"score={actual:.1f} {'∈' if passed else '∉'} [{lo}, {hi}]"
    return passed, msg


def grade_field_match(actual: Any, expected: Any) -> tuple[bool, str]:
    """Exact match on a field value."""
    passed = actual == expected
    msg = f"actual={actual!r} {'==' if passed else '!='} expected={expected!r}"
    return passed, msg


def grade_enum_in(actual: Any, allowed: list) -> tuple[bool, str]:
    """Check that actual value is in the allowed set."""
    passed = actual in allowed
    msg = f"actual={actual!r} {'∈' if passed else '∉'} {allowed}"
    return passed, msg


def grade_list_contains(actual: list, required: list) -> tuple[bool, str]:
    """Check that all required items appear in the actual list."""
    missing = [r for r in required if r not in actual]
    passed = len(missing) == 0
    msg = f"missing={missing}" if missing else "all required items present"
    return passed, msg


def grade_bool(actual: bool, expected: bool) -> tuple[bool, str]:
    """Check boolean match."""
    passed = actual == expected
    msg = f"actual={actual} {'==' if passed else '!='} expected={expected}"
    return passed, msg


def classify_detection(expected_signal: bool, actual_signal: bool) -> str:
    """Classify a detection result into TP/FP/FN/TN.

    Returns a business-friendly Chinese label:
      正确标记 (TP) — expected=True,  actual=True
      漏报     (FN) — expected=True,  actual=False
      误报     (FP) — expected=False, actual=True
      正确放行 (TN) — expected=False, actual=False
    """
    if expected_signal and actual_signal:
        return "正确标记"
    elif expected_signal and not actual_signal:
        return "漏报"
    elif not expected_signal and actual_signal:
        return "误报"
    else:
        return "正确放行"


def grade_case(actual_output: dict, expect: dict) -> list[tuple[str, bool, str]]:
    """Run all applicable graders for a single eval case.

    Returns list of (check_name, passed, message) tuples.
    """
    results: list[tuple[str, bool, str]] = []

    for key, expected_val in expect.items():
        if key.endswith("_range"):
            # e.g. template_score_range → check actual_output["template_score"]
            field_name = key[:-6]  # strip "_range"
            actual_val = actual_output.get(field_name)
            if actual_val is None:
                results.append((key, False, f"field '{field_name}' not found in output"))
            else:
                passed, msg = grade_score_range(float(actual_val), expected_val)
                results.append((key, passed, msg))

        elif key == "has_signal":
            actual_val = actual_output.get("has_signal", False)
            passed, msg = grade_bool(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key == "rule_name":
            actual_val = actual_output.get("rule_name")
            passed, msg = grade_field_match(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key == "layer":
            actual_val = actual_output.get("layer")
            passed, msg = grade_field_match(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key == "recommendation":
            actual_val = actual_output.get("recommendation")
            passed, msg = grade_field_match(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key == "recommendation_in":
            actual_val = actual_output.get("recommendation")
            passed, msg = grade_enum_in(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key == "triggered_contains":
            actual_val = actual_output.get("triggered_factors", [])
            passed, msg = grade_list_contains(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key in ("contradiction_found", "person_amount_reasonable"):
            actual_val = actual_output.get(key)
            passed, msg = grade_field_match(actual_val, expected_val)
            results.append((key, passed, msg))

        elif key.endswith("_range") is False and key not in (
            "http_status", "tool_calls_include", "tool_calls_exclude",
            "response_contains", "response_quality",
            "whitelist_error_contains", "blocked_tool",
            "green_flags_min", "red_flags_min", "advisory_contains",
        ):
            # Generic field match for any other expected field
            actual_val = actual_output.get(key)
            if actual_val is not None:
                passed, msg = grade_field_match(actual_val, expected_val)
                results.append((key, passed, msg))

    return results


def event_tool_names(events: list[dict]) -> list[str]:
    """Return tool_call names in the order they appeared."""
    return [e.get("name", "") for e in events if e.get("type") == "tool_call"]


def event_subagents(events: list[dict]) -> list[str]:
    """Return subagent names observed in subagent_step/tool_call events."""
    names: list[str] = []
    for event in events:
        if event.get("type") in {"subagent_step", "tool_call", "tool_result"}:
            name = event.get("subagent")
            if name and name not in names:
                names.append(str(name))
    return names


def assistant_text(events: list[dict]) -> str:
    """Concatenate streamed assistant text events for response graders."""
    return " ".join(e.get("text", "") for e in events if e.get("type") == "assistant_text")


def grade_must_call_tools(events: list[dict], required: list[str]) -> tuple[bool, str]:
    """Binary check: every required tool was called at least once."""
    names = event_tool_names(events)
    missing = [name for name in required if name not in names]
    passed = not missing
    return passed, f"missing={missing}; called={names}"


def grade_forbidden_tools_absent(events: list[dict], forbidden: list[str]) -> tuple[bool, str]:
    """Binary check: forbidden tools never appeared."""
    names = event_tool_names(events)
    present = [name for name in forbidden if name in names]
    passed = not present
    return passed, f"present={present}; called={names}"


def grade_required_subagents(events: list[dict], required: list[str]) -> tuple[bool, str]:
    """Binary check: expected subagent nodes appeared in the trajectory."""
    names = event_subagents(events)
    missing = [name for name in required if name not in names]
    passed = not missing
    return passed, f"missing={missing}; subagents={names}"


def grade_agent_trace_present(events: list[dict], expected: bool = True) -> tuple[bool, str]:
    """Binary check: run_agent emitted the aggregate agent_trace event."""
    present = any(e.get("type") == "agent_trace" for e in events)
    passed = present == expected
    return passed, f"agent_trace_present={present}"


def grade_response_contains(events: list[dict], required_phrases: list[str]) -> tuple[bool, str]:
    """Binary check: final streamed answer contains all required phrases."""
    text = assistant_text(events)
    missing = [phrase for phrase in required_phrases if phrase not in text]
    passed = not missing
    return passed, f"missing={missing}; text={text[:240]!r}"


def grade_response_excludes(events: list[dict], forbidden_phrases: list[str]) -> tuple[bool, str]:
    """Binary check: final streamed answer contains none of the forbidden phrases."""
    text = assistant_text(events)
    present = [phrase for phrase in forbidden_phrases if phrase in text]
    passed = not present
    return passed, f"present={present}; text={text[:240]!r}"


def grade_final_fields(draft_fields: dict, expected_fields: dict) -> tuple[bool, str]:
    """Binary check: final draft fields match expected values.

    Numeric fields tolerate string/float representation differences.
    String fields use exact match unless expected value is a substring marker
    handled by the caller.
    """
    mismatches = []
    for field, expected in (expected_fields or {}).items():
        actual = draft_fields.get(field)
        if isinstance(expected, (int, float)):
            try:
                ok = abs(float(actual) - float(expected)) < 0.0001
            except (TypeError, ValueError):
                ok = False
        else:
            ok = actual == expected
        if not ok:
            mismatches.append({"field": field, "actual": actual, "expected": expected})
    passed = not mismatches
    return passed, f"mismatches={mismatches}"


def grade_fields_absent(draft_fields: dict, absent_fields: list[str]) -> tuple[bool, str]:
    """Binary check: fields that should not be written are absent."""
    present = [field for field in absent_fields if field in (draft_fields or {})]
    passed = not present
    return passed, f"present={present}; fields={draft_fields}"


def grade_field_sources_include(field_sources: dict, expected_sources: dict) -> tuple[bool, str]:
    """Binary check: selected fields carry expected provenance/source strings."""
    mismatches = []
    for field, expected in (expected_sources or {}).items():
        actual = (field_sources or {}).get(field)
        if expected not in str(actual):
            mismatches.append({"field": field, "actual": actual, "expected_contains": expected})
    passed = not mismatches
    return passed, f"mismatches={mismatches}"


def grade_tool_args(events: list[dict], expected_args: dict[str, dict]) -> tuple[bool, str]:
    """Binary check: at least one call per named tool contains expected args.

    The expected dict is partial: only listed keys are checked. For fixture_id
    expectations, real models may query by business fields instead of naming the
    fixture directly; in that case we also accept the paired tool_result
    fixture_id as an equivalent match.
    """
    mismatches = []
    for tool_name, expected in (expected_args or {}).items():
        calls = [
            e.get("input") or {}
            for e in events
            if e.get("type") == "tool_call" and e.get("name") == tool_name
        ]
        matched = False
        for call in calls:
            if all(call.get(k) == v for k, v in expected.items()):
                matched = True
                break
        expected_fixture = expected.get("fixture_id")
        if not matched and expected_fixture:
            for event in events:
                if event.get("type") != "tool_result" or event.get("name") != tool_name:
                    continue
                result = event.get("result") or {}
                if isinstance(result, dict) and result.get("fixture_id") == expected_fixture:
                    matched = True
                    break
        if not matched:
            mismatches.append({"tool": tool_name, "expected_subset": expected, "calls": calls})
    passed = not mismatches
    return passed, f"mismatches={mismatches}"


def grade_trace_shape(events: list[dict], required_tools: list[str]) -> tuple[bool, str]:
    """Layer 4: verify every required tool call has a complete trace pair.

    For each tool in required_tools, checks that the events contain:
      1. A tool_call event with {type, name, input, subagent, connectors}
      2. A matching tool_result event with {type, name, result, subagent, connectors, output_summary}
      3. A subagent_step event for both call and result phases

    This ensures the trace is complete and reproducible for debugging.
    """
    errors = []
    for tool_name in required_tools:
        calls = [e for e in events if e.get("type") == "tool_call" and e.get("name") == tool_name]
        results = [e for e in events if e.get("type") == "tool_result" and e.get("name") == tool_name]
        steps = [e for e in events if e.get("type") == "subagent_step" and e.get("tool") == tool_name]

        if not calls:
            errors.append(f"{tool_name}: no tool_call event")
            continue

        for call in calls:
            missing = [k for k in ("name", "input", "subagent", "connectors") if k not in call]
            if missing:
                errors.append(f"{tool_name} tool_call: missing fields {missing}")

        if not results:
            is_deferred = any(
                e.get("type") == "tool_result" and e.get("name") == tool_name
                and isinstance(e.get("result"), dict) and e["result"].get("deferred_to")
                for e in events
            )
            if not is_deferred:
                errors.append(f"{tool_name}: no tool_result event")
        else:
            for res in results:
                if "result" not in res:
                    errors.append(f"{tool_name} tool_result: missing 'result' field")
                if "subagent" not in res:
                    errors.append(f"{tool_name} tool_result: missing 'subagent' field")
                if "connectors" not in res:
                    errors.append(f"{tool_name} tool_result: missing 'connectors' field")

        call_steps = [s for s in steps if s.get("event") == "tool_call"]
        result_steps = [s for s in steps if s.get("event") == "tool_result"]
        if not call_steps:
            errors.append(f"{tool_name}: no subagent_step(tool_call)")
        if not result_steps:
            is_write = tool_name in ("update_draft_field", "update_report_line_field")
            if not is_write:
                errors.append(f"{tool_name}: no subagent_step(tool_result)")

    passed = not errors
    return passed, "; ".join(errors) if errors else "all trace shapes valid"
