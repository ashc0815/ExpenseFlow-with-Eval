from __future__ import annotations

import asyncio

import pytest

from backend.services import didi_provider


def test_local_mock_lookup_matches_known_didi_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIDI_PROVIDER", raising=False)

    result = asyncio.run(didi_provider.lookup_didi_trip({"date": "2026-05-08", "amount": 86, "city": "上海"}))

    assert result["provider"] == "local_mock"
    assert result["source"] == "didi_mock"
    assert result["confidence"] == 0.95
    assert result["candidates"][0]["merchant"] == "滴滴出行"
    assert result["candidates"][0]["from"] == "公司"
    assert result["candidates"][0]["to"] == "虹桥机场"


@pytest.mark.asyncio
async def test_mcp_sandbox_requires_url_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIDI_PROVIDER", "mcp_sandbox")
    monkeypatch.delenv("DIDI_MCP_URL", raising=False)
    monkeypatch.delenv("DIDI_MCP_KEY", raising=False)

    result = await didi_provider.lookup_didi_trip({"order_id": "sandbox-order"})

    assert result["provider"] == "mcp_sandbox"
    assert result["confidence"] == 0.0
    assert "DIDI_MCP_URL or DIDI_MCP_KEY" in result["error"]


@pytest.mark.asyncio
async def test_mcp_sandbox_call_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIDI_PROVIDER", "mcp_sandbox")
    monkeypatch.setenv("DIDI_MCP_URL", "https://example.test/mcp-servers-sandbox?key=fake")

    seen: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "jsonrpc": "2.0",
                "id": "fake",
                "result": {
                    "content": [{"type": "text", "text": "行程完成，费用约75元"}],
                    "structuredContent": {
                        "orderId": "sandbox-order",
                        "statusText": "行程完成",
                        "from": {"name": "北京西站"},
                        "to": {"name": "西二旗地铁站"},
                        "priceText": "75元",
                    },
                },
            }

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            seen["timeout"] = timeout

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, headers: dict, json: dict) -> FakeResponse:
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(didi_provider.httpx, "AsyncClient", FakeClient)

    result = await didi_provider.lookup_didi_trip(
        {"order_id": "sandbox-order", "amount": 75, "city": "北京"}
    )

    assert seen["json"]["method"] == "tools/call"
    assert seen["json"]["params"]["name"] == "taxi_query_order"
    assert seen["json"]["params"]["arguments"] == {"order_id": "sandbox-order"}
    assert result["provider"] == "mcp_sandbox"
    assert result["confidence"] == 0.9
    assert result["candidates"][0]["trip_id"] == "sandbox-order"
    assert result["candidates"][0]["amount"] == 75.0
    assert result["candidates"][0]["from"] == "北京西站"
