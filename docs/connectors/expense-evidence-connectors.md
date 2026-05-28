# Expense Evidence Connectors

> **Status:** Connector registry for the employee reimbursement assistant.

Connectors are the governed data entry points behind tools. Subagents do not
call connectors directly; tools call connectors and normalize their responses.

---

## Connector Registry

| Connector | Provider modes | Tools using it | Purpose |
|---|---|---|---|
| `ocr-provider` | `real`, `replay`, `mock`, `stub`, `error` | `extract_receipt_fields` | OCR and receipt field extraction. |
| `ctrip-evidence-provider` | `mock`, `sandbox`, `real`, `stub`, `error` | `lookup_ctrip_booking` | Ctrip/Trip.com-style hotel, flight, train, and travel booking evidence. |
| `didi-evidence-provider` | `mock`, `sandbox`, `real`, `stub`, `error` | `lookup_didi_trip` | Didi-style ride, route, fare, invoice, and payment evidence. |
| `card-transaction-provider` | `mock`, `sandbox`, `real`, `stub`, `error` | `lookup_card_transaction` | Corporate or employee card transaction evidence. |
| `expense-policy-store` | `local`, `mock`, `real`, `stub` | `get_policy_rules` | Policy limits, city tiers, tolerance, invoice requirements, and approval rules. |
| `duplicate-expense-ledger` | `local`, `mock`, `real` | `check_duplicate_invoice` | Duplicate invoice/order/trip/transaction lookup. |
| `category-classifier` | `local`, `mock`, `real` | `suggest_category` | Category suggestion from normalized claim and evidence. |
| `draft-store` | `local`, `real` | `update_draft_field` | Safe mutation target for reimbursement drafts. |
| `trace-sink` | `sqlite`, `jsonl`, `warehouse`, `none` | all connector-backed tools | Replayable request/response audit log. |

---

## Environment Modes

Use independent provider modes. Do not use one global "real vs mock" switch.

```text
AGENT_LLM_PROVIDER=real
OCR_PROVIDER=real|replay|mock|stub|error
CTRIP_PROVIDER=mock|sandbox|real|stub|error
DIDI_PROVIDER=mock|sandbox|real|stub|error
CARD_PROVIDER=mock|sandbox|real|stub|error
POLICY_PROVIDER=local|mock|real|stub
DUPLICATE_PROVIDER=local|mock|real
CATEGORY_PROVIDER=local|mock|real
DRAFT_PROVIDER=local|real
TRACE_SINK=sqlite|jsonl|warehouse|none
```

Recommended eval mode:

```text
AGENT_LLM_PROVIDER=real
OCR_PROVIDER=replay
CTRIP_PROVIDER=mock
DIDI_PROVIDER=mock
CARD_PROVIDER=mock
POLICY_PROVIDER=local
DUPLICATE_PROVIDER=local
TRACE_SINK=sqlite
```

This mode keeps tool calling and reasoning real while making external evidence
deterministic and replayable.

---

## Normalized Connector Result

Every connector-backed tool returns a normalized envelope.

```json
{
  "status": "ok",
  "connector": "ctrip-evidence-provider",
  "provider_name": "ctrip_mock",
  "provider_mode": "mock",
  "fixture_id": "ctrip_hotel_shenzhen_680",
  "request_normalized": {},
  "response_normalized": {},
  "candidates": [],
  "candidate_count": 0,
  "error": null
}
```

Allowed statuses:

```text
ok
not_found
multiple_candidates
missing_fields
conflict
not_configured
provider_error
timeout
auth_error
rate_limited
malformed_response
```

`not_configured`, `timeout`, `auth_error`, `rate_limited`, and
`provider_error` are availability states. They must not be collapsed into
`not_found`.

---

## Trace Requirements

Every connector-backed call must write one trace row unless `TRACE_SINK=none`.
Eval should not use `TRACE_SINK=none`.

```json
{
  "trace_id": "trace_card_001",
  "conversation_turn_id": "turn_001",
  "draft_id": "draft_001",
  "employee_id": "emp_dev",
  "subagent": "evidence-reconciler",
  "skills": ["external-evidence-reconciliation", "provider-traceability"],
  "tool_name": "lookup_card_transaction",
  "connector": "card-transaction-provider",
  "provider_name": "card_mock",
  "provider_mode": "mock",
  "fixture_id": "card_shenzhen_hotel_680",
  "attempt_index": 2,
  "request_normalized": {},
  "response_normalized": {},
  "candidate_count": 1,
  "selected_candidate_id": "CARD-MOCK-001",
  "reconciliation_status": "can_write_draft",
  "reconciliation_labels": ["ctrip_card_match"],
  "fields_written": ["amount", "date"],
  "latency_ms": 8,
  "error": null,
  "created_at": "2026-05-18T00:00:00Z"
}
```

---

## Connector Safety Rules

- Mock fixtures are first-class eval inputs, not fallback hacks.
- Providers must return structured errors instead of throwing raw SDK errors to
  the agent loop.
- Fixtures must be deterministic in CI.
- Sandbox or real responses can be recorded into replay fixtures, but replayed
  eval should not depend on live vendor availability.
- Tools should normalize provider-specific terms into ExpenseFlow terms:
  booking, trip, transaction, invoice, refund, net amount, status, candidate.
- Connector responses should avoid exposing credentials, raw tokens, or
  unnecessary PII in traces.
