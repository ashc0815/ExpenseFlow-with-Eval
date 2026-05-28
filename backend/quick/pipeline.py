"""Deterministic OCR→classify→dedupe→budget pipeline for the quick flow."""
from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.middleware.auth import UserContext
from backend.agents.expense_assistant.tool_handlers import (
    tool_extract_receipt_fields,
    tool_suggest_category,
    tool_check_duplicate_invoice,
    tool_get_budget_summary,
)
from backend.db.store import create_audit_log, get_draft, update_draft_field
from backend.quick.layer_decision import decide_layer
from backend.services.pattern_miner import apply_rules_to_draft


async def _set(db: AsyncSession, draft_id: str, field: str, value, source="pipeline"):
    if value is not None and value != "":
        await update_draft_field(db, draft_id, field, value, source)


async def run_quick_pipeline(
    draft_id: str,
    ctx: UserContext,
    db: AsyncSession,
) -> AsyncIterator[dict]:
    """Yield SSE-style event dicts (no `data:` prefix). Caller formats them."""
    draft = await get_draft(db, draft_id)
    if not draft:
        yield {"type": "error", "message": "draft not found"}
        return

    # Mark entry early so abandoned drafts still show "quick"
    draft.entry = "quick"
    await db.commit()

    # ── 1. OCR ─────────────────────────────────────────────────
    try:
        ocr = await tool_extract_receipt_fields({}, ctx, db, draft_id)
    except Exception as exc:  # noqa: BLE001
        ocr = {"error": str(exc)}

    if ocr.get("error"):
        yield {"type": "ocr_failed", "error": ocr["error"]}
        # Hard error — force layer 3_hard, skip rest
        fresh = await get_draft(db, draft_id)
        fresh.layer = "3_hard"
        await db.commit()
        yield {"type": "card_ready", "layer": "3_hard", "actions": ["redirect"]}
        return

    await _set(db, draft_id, "amount", ocr.get("amount"), "ocr")
    await _set(db, draft_id, "merchant", ocr.get("merchant"), "ocr")
    await _set(db, draft_id, "date", ocr.get("date"), "ocr")
    await _set(db, draft_id, "invoice_number", ocr.get("invoice_number"), "ocr")
    await _set(db, draft_id, "tax_amount", ocr.get("tax_amount"), "ocr")
    await _set(db, draft_id, "currency", ocr.get("currency"), "ocr")

    yield {
        "type": "ocr_done",
        "amount": ocr.get("amount"),
        "merchant": ocr.get("merchant"),
        "date": ocr.get("date"),
        "confidence": ocr.get("confidence") or (0.95 if ocr.get("amount") else 0.0),
    }

    # ── 2. Classify ────────────────────────────────────────────
    classify = await tool_suggest_category(
        {"merchant": ocr.get("merchant") or ""}, ctx, db, draft_id,
    )
    await _set(db, draft_id, "category", classify.get("category"), "pipeline")
    yield {
        "type": "classify_done",
        "category": classify.get("category"),
        "confidence": classify.get("confidence"),
    }

    # ── 2.5 Auto-rule application ─────────────────────────────
    # Personal rules the user already accepted (#5 user-behavior learning).
    # These outrank the classifier — the user explicitly told us "when
    # merchant=X, set field=Y" — so they overwrite by default.
    try:
        applied = await apply_rules_to_draft(
            db, employee_id=ctx.user_id, draft_id=draft_id,
            merchant=ocr.get("merchant"),
        )
    except Exception:  # noqa: BLE001 — pipeline must keep running
        applied = []
    if applied:
        await create_audit_log(
            db, actor_id=ctx.user_id, action="auto_rules_applied",
            resource_type="draft", resource_id=draft_id,
            detail={"merchant": ocr.get("merchant"), "rules": applied},
        )
        yield {"type": "auto_rules_applied", "rules": applied}

    # ── 3. Dedupe ──────────────────────────────────────────────
    if ocr.get("invoice_number"):
        dedupe = await tool_check_duplicate_invoice(
            {"invoice_number": ocr["invoice_number"]}, ctx, db, draft_id,
        )
    else:
        dedupe = {"is_duplicate": False}
    yield {"type": "dedupe_done", "is_duplicate": dedupe.get("is_duplicate", False)}

    # ── 4. Budget ──────────────────────────────────────────────
    budget = await tool_get_budget_summary({}, ctx, db, draft_id)
    yield {"type": "budget_done", "signal": budget.get("signal", "ok")}

    # ── 5. Decide layer and persist ────────────────────────────
    fresh = await get_draft(db, draft_id)
    missing_optional: list[str] = []
    if not (fresh.fields or {}).get("project_code"):
        missing_optional.append("project_code")

    layer = decide_layer(
        ocr={**ocr, "confidence": ocr.get("confidence") or 0.95},
        classify=classify,
        dedupe=dedupe,
        budget=budget,
        missing_optional_fields=missing_optional,
    )
    fresh.layer = layer
    await db.commit()

    actions = {"1": ["attest"], "2": ["attest", "edit"],
               "3_soft": ["manual", "retake"], "3_hard": ["redirect"]}[layer]
    yield {"type": "card_ready", "layer": layer, "actions": actions}
