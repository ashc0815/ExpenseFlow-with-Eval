"""Didi evidence lookup provider.

The chat agent should see one business-level tool: ``lookup_didi_trip``.
This module lets that tool run against either deterministic local fixtures
for evals or Didi's real MCP sandbox endpoint for integration smoke tests.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Optional

import httpx

from backend.services.external_evidence_fixtures import lookup_fixture_evidence


def _provider_mode() -> str:
    return os.getenv("DIDI_PROVIDER", "local_mock").strip().lower()


def _mcp_url() -> Optional[str]:
    url = os.getenv("DIDI_MCP_URL", "").strip()
    if url:
        return url
    key = os.getenv("DIDI_MCP_KEY", "").strip()
    if key:
        return f"https://mcp.didichuxing.com/mcp-servers-sandbox?key={key}"
    return None


async def lookup_didi_trip(args: dict) -> dict:
    """Lookup Didi trip evidence using the configured provider."""
    mode = _provider_mode()
    if mode in {"mcp", "mcp_sandbox", "didi_mcp", "didi_mcp_sandbox"}:
        return await _lookup_didi_trip_mcp_sandbox(args)
    return _lookup_didi_trip_local(args)


def _lookup_didi_trip_local(args: dict) -> dict:
    return lookup_fixture_evidence("didi", args)


async def _lookup_didi_trip_mcp_sandbox(args: dict) -> dict:
    """Call Didi's Streamable HTTP MCP sandbox and normalize the response.

    Didi MCP's public sandbox is designed around taxi order lifecycle tools.
    It is useful as a real MCP protocol integration test, while local fixtures
    remain the deterministic source for historical reimbursement eval cases.
    """
    url = _mcp_url()
    if not url:
        return {
            "source": "didi_mcp_sandbox",
            "provider": "mcp_sandbox",
            "query": args,
            "candidates": [],
            "confidence": 0.0,
            "error": "DIDI_MCP_URL or DIDI_MCP_KEY is required when DIDI_PROVIDER=mcp_sandbox",
        }

    tool_name = os.getenv("DIDI_MCP_LOOKUP_TOOL", "taxi_query_order").strip() or "taxi_query_order"
    tool_args = _mcp_tool_args(args, tool_name)
    request_body = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": uuid.uuid4().hex,
        "params": {
            "name": tool_name,
            "arguments": tool_args,
        },
    }

    timeout = float(os.getenv("DIDI_MCP_TIMEOUT_SECONDS", "8"))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json; charset=utf-8"},
                json=request_body,
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "didi_mcp_sandbox",
            "provider": "mcp_sandbox",
            "query": args,
            "mcp_tool": tool_name,
            "candidates": [],
            "confidence": 0.0,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if payload.get("error"):
        return {
            "source": "didi_mcp_sandbox",
            "provider": "mcp_sandbox",
            "query": args,
            "mcp_tool": tool_name,
            "candidates": [],
            "confidence": 0.0,
            "error": payload["error"],
            "raw": payload,
        }

    result = payload.get("result") or {}
    candidate = _normalize_mcp_trip_candidate(result, args)
    candidates = [candidate] if candidate else []
    return {
        "source": "didi_mcp_sandbox",
        "provider": "mcp_sandbox",
        "query": args,
        "mcp_tool": tool_name,
        "mcp_arguments": tool_args,
        "candidates": candidates,
        "confidence": _mcp_confidence(candidate, args),
        "raw": result,
    }


def _mcp_tool_args(args: dict, tool_name: str) -> dict:
    if tool_name == "taxi_query_order":
        order_id = args.get("order_id") or args.get("trip_id")
        return {"order_id": str(order_id)} if order_id else {}
    return dict(args)


def _normalize_mcp_trip_candidate(result: dict, query: dict) -> Optional[dict]:
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        structured = {}

    content_text = _content_text(result)
    text_data = _parse_jsonish(content_text)
    if isinstance(text_data, dict):
        structured = {**text_data, **structured}

    if not structured and not content_text:
        return None

    from_obj = structured.get("from") if isinstance(structured.get("from"), dict) else {}
    to_obj = structured.get("to") if isinstance(structured.get("to"), dict) else {}
    amount = _extract_amount(structured, content_text)

    candidate = {
        "trip_id": structured.get("orderId")
        or structured.get("order_id")
        or query.get("order_id")
        or query.get("trip_id"),
        "date": query.get("date"),
        "merchant": "滴滴出行",
        "amount": amount,
        "currency": structured.get("currency") or "CNY",
        "city": query.get("city"),
        "from": from_obj.get("name") or structured.get("from_name") or structured.get("fromName"),
        "to": to_obj.get("name") or structured.get("to_name") or structured.get("toName"),
        "status": structured.get("statusText") or structured.get("status") or structured.get("statusCode"),
        "invoice_status": structured.get("invoiceStatus") or structured.get("invoice_status"),
        "invoice_available": None,
        "mcp_summary": content_text,
    }
    return {k: v for k, v in candidate.items() if v is not None}


def _content_text(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return str(first.get("text") or "")
        return str(first)
    return ""


def _parse_jsonish(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _extract_amount(structured: dict, content_text: str) -> Optional[float]:
    for key in ("amount", "price", "fee", "totalFee", "actualPrice"):
        if structured.get(key) is not None:
            return _to_float(structured.get(key))

    price_text = structured.get("priceText") or structured.get("price_text")
    if price_text:
        return _to_float(price_text)

    match = re.search(r"(\d+(?:\.\d+)?)\s*元", content_text or "")
    if match:
        return _to_float(match.group(1))
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _mcp_confidence(candidate: Optional[dict], query: dict) -> float:
    if not candidate:
        return 0.0
    amount_arg = query.get("amount")
    amount = candidate.get("amount")
    if amount_arg is not None and amount is not None:
        try:
            return 0.9 if abs(float(amount_arg) - float(amount)) <= 1.0 else 0.35
        except (TypeError, ValueError):
            return 0.55
    # MCP sandbox validates protocol/tool connectivity, but its taxi lifecycle
    # tools usually do not prove historical receipt date+amount by themselves.
    return 0.6
