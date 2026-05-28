"""Layer 1: Provider/Tool Unit Eval.

Tests mock provider fixtures directly — no LLM involved.
Validates that lookup tools return expected candidates, confidence scores,
and correctly handle match / no-match / multi-match / cancelled / rebooked.

Run:
  pytest backend/tests/test_provider_unit.py -v
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
_DB_URL = f"sqlite+aiosqlite:///{_TMP_DB.name}"

os.environ.setdefault("DATABASE_URL", _DB_URL)
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("UPLOAD_DIR", "/tmp/expenseflow_provider_unit_eval")

from backend.api.middleware.auth import UserContext
from backend.api.routes.chat import (
    tool_lookup_card_transaction,
    tool_lookup_ctrip_booking,
    tool_lookup_didi_trip,
)
from backend.db.store import Base

_engine = create_async_engine(_DB_URL)
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_CTX = UserContext(user_id="provider-unit-test", roles=["employee"])


def setup_module(_: Any) -> None:
    asyncio.new_event_loop().run_until_complete(
        _engine.begin().__aenter__().then(lambda conn: conn.run_sync(Base.metadata.create_all))
    ) if False else asyncio.new_event_loop().run_until_complete(_init_db())


async def _init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def teardown_module(_: Any) -> None:
    try:
        asyncio.new_event_loop().run_until_complete(_engine.dispose())
    except Exception:
        pass
    try:
        os.unlink(_TMP_DB.name)
    except PermissionError:
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _call(tool_fn, args: dict) -> dict:
    async with _Session() as db:
        return await tool_fn(args, _CTX, db, "draft-unit-test")


# ── Ctrip Booking ──────────────────────────────────────────────────────────


class TestCtripBooking:

    def test_unique_flight_match(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "date": "2026-05-08", "amount": 1280, "booking_type": "flight",
        }))
        assert result["source"] == "ctrip_mock"
        assert len(result["candidates"]) == 1
        assert result["confidence"] == 0.95
        c = result["candidates"][0]
        assert c["booking_id"] == "CTRIP-FLIGHT-SHA-SZX-1280"
        assert result["provider_mode"] == "mock"
        assert result["request_normalized"]["date"] == "2026-05-08"
        assert c["status"] == "active"
        assert c["amount"] == 1280.0

    def test_hotel_match_excludes_rebooked(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "date": "2026-05-09", "amount": 680, "booking_type": "hotel",
        }))
        ids = [c["booking_id"] for c in result["candidates"]]
        assert "CTRIP-HOTEL-SZ-680" in ids
        assert "CTRIP-HOTEL-OLD-001" not in ids, "rebooked should be excluded without booking_id"

    def test_cancelled_booking_returns_status(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "date": "2026-05-10", "amount": 900, "booking_type": "hotel",
        }))
        assert len(result["candidates"]) == 1
        c = result["candidates"][0]
        assert c["status"] == "cancelled"
        assert c["net_amount"] == 0.0
        assert c["refund_amount"] == 900.0

    def test_no_match_returns_empty(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "date": "2026-12-01", "amount": 9999,
        }))
        assert result["candidates"] == []
        assert result["confidence"] == 0.0

    def test_amount_mismatch_falls_back_to_loose(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "date": "2026-05-08", "amount": 1300, "booking_type": "flight",
        }))
        assert len(result["candidates"]) == 1, "loose match should find flight-001 by date+type"
        assert result["candidates"][0]["amount"] == 1280.0

    def test_rebooked_found_by_booking_id(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "booking_id": "ctrip-hotel-old-001",
        }))
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["status"] == "rebooked"
        assert result["candidates"][0]["current_booking_id"] == "CTRIP-HOTEL-SZ-680"

    def test_direct_fixture_id_returns_conflict_fixture_without_filtering(self):
        result = _run(_call(tool_lookup_ctrip_booking, {
            "fixture_id": "ctrip_conflict_amount_hotel_680_claim_760",
            "date": "2026-05-09",
            "amount": 760,
            "booking_type": "hotel",
        }))
        assert result["fixture_id"] == "ctrip_conflict_amount_hotel_680_claim_760"
        assert result["status"] == "ok"
        assert result["candidates"][0]["amount"] == 680.0


# ── Didi Trip ──────────────────────────────────────────────────────────────


class TestDidiTrip:

    def test_unique_match_by_date_amount_city(self):
        result = _run(_call(tool_lookup_didi_trip, {
            "date": "2026-05-08", "amount": 86, "city": "上海",
        }))
        assert result["source"] == "didi_mock"
        assert len(result["candidates"]) == 1
        assert result["confidence"] == 0.95
        c = result["candidates"][0]
        assert c["trip_id"] == "DIDI-SH-AIRPORT-86"
        assert c["city"] == "上海"

    def test_match_by_amount_only(self):
        result = _run(_call(tool_lookup_didi_trip, {"amount": 54}))
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["trip_id"] == "DIDI-SZ-HOTEL-54"

    def test_no_match_wrong_city(self):
        result = _run(_call(tool_lookup_didi_trip, {
            "date": "2026-05-08", "amount": 86, "city": "广州",
        }))
        assert result["candidates"] == []
        assert result["confidence"] == 0.0

    def test_no_match_wrong_date(self):
        result = _run(_call(tool_lookup_didi_trip, {
            "date": "2026-05-15", "amount": 200,
        }))
        assert result["candidates"] == []

    def test_multiple_candidates_on_broad_query(self):
        result = _run(_call(tool_lookup_didi_trip, {}))
        assert len(result["candidates"]) == 2
        assert result["confidence"] == 0.55


# ── Card Transaction ──────────────────────────────────────────────────────


class TestCardTransaction:

    def test_unique_match_didi(self):
        result = _run(_call(tool_lookup_card_transaction, {
            "date": "2026-05-08", "amount": 86, "merchant_hint": "DIDI",
        }))
        assert result["source"] == "card_mock"
        assert len(result["candidates"]) == 1
        assert result["confidence"] == 0.95
        assert result["candidates"][0]["transaction_id"] == "CARD-DIDI-SH-86"

    def test_unique_match_ctrip(self):
        result = _run(_call(tool_lookup_card_transaction, {
            "date": "2026-05-08", "amount": 1280, "merchant_hint": "CTRIP",
        }))
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["transaction_id"] == "CARD-CTRIP-FLIGHT-1280"

    def test_case_id_selects_domain_fixture(self):
        result = _run(_call(tool_lookup_card_transaction, {
            "case_id": "conflict_005_card_merchant_unrelated_blocks_write",
            "date": "2026-05-09",
            "amount": 680,
            "merchant_hint": "HOTEL",
        }))
        assert result["case_id"] == "conflict_005_card_merchant_unrelated_blocks_write"
        assert result["fixture_id"] == "card_conflict_merchant_unrelated"
        assert result["candidates"][0]["merchant"] == "APPLE STORE"

    def test_multiple_same_date(self):
        result = _run(_call(tool_lookup_card_transaction, {
            "date": "2026-05-08",
        }))
        assert len(result["candidates"]) == 2
        assert result["confidence"] == 0.55

    def test_no_match_amount(self):
        result = _run(_call(tool_lookup_card_transaction, {"amount": 300}))
        assert result["candidates"] == []
        assert result["confidence"] == 0.0

    def test_no_match_future_date(self):
        result = _run(_call(tool_lookup_card_transaction, {
            "date": "2026-05-10", "amount": 54,
        }))
        assert result["candidates"] == []


# ── Cross-provider consistency ────────────────────────────────────────────


class TestCrossProviderConsistency:
    """Verify that matching fixtures across providers agree on amounts."""

    def test_didi_card_amount_agree(self):
        didi = _run(_call(tool_lookup_didi_trip, {"date": "2026-05-08", "amount": 86}))
        card = _run(_call(tool_lookup_card_transaction, {"date": "2026-05-08", "amount": 86, "merchant_hint": "DIDI"}))
        assert didi["candidates"][0]["amount"] == card["candidates"][0]["amount"]

    def test_ctrip_card_amount_agree(self):
        ctrip = _run(_call(tool_lookup_ctrip_booking, {"date": "2026-05-08", "amount": 1280, "booking_type": "flight"}))
        card = _run(_call(tool_lookup_card_transaction, {"date": "2026-05-08", "amount": 1280, "merchant_hint": "CTRIP"}))
        assert ctrip["candidates"][0]["amount"] == card["candidates"][0]["amount"]

    def test_hotel_card_amount_agree(self):
        ctrip = _run(_call(tool_lookup_ctrip_booking, {"date": "2026-05-09", "amount": 680, "booking_type": "hotel"}))
        card = _run(_call(tool_lookup_card_transaction, {"date": "2026-05-09", "amount": 680}))
        assert ctrip["candidates"][0]["amount"] == card["candidates"][0]["amount"]
