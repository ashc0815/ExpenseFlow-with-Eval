# Expense Assistant Subagent Tool Call Design

> **Status:** Implementation guide for the employee reimbursement assistant.
>
> This document turns the Ctrip, Didi, and card reconciliation discussion into a
> usable tool-call and eval contract. It assumes the LLM/OCR/tool-calling loop is
> real, while Ctrip, Didi, and card evidence can be served by deterministic mock
> providers.

---

## TL;DR

ExpenseFlow should treat reimbursement completion as a governed finance-agent
workflow:

1. Keep the orchestration simple and composable.
2. Give each subagent a narrow tool allowlist.
3. Treat tool results as environmental ground truth, not model intuition.
4. Record every external evidence request and response.
5. Stop and ask the employee when evidence is missing, conflicting, repeated, or
   unsafe to write.
6. Only write draft fields after a reconciliation decision says the evidence is
   sufficient.

The practical shape:

```text
receipt-reader
  -> extracts the employee's claim from OCR + user text

evidence-reconciler
  -> calls Ctrip, Didi, card, policy, and duplicate-check tools
  -> returns an evidence bundle and reconciliation decision

draft-writer
  -> writes draft fields only when the decision is can_write_draft
  -> otherwise asks for clarification or explains the blocker
```

This mirrors the best parts of finance-agent patterns: governed connectors,
specialized agents, auditable tool calls, and human checkpoints for blockers.

---

## Design Principles

These principles are adapted to ExpenseFlow from Anthropic's public agent and
finance-agent guidance:

- **Simple loops beat heavy frameworks.** Use one orchestrator loop that routes
  tool calls to narrowly scoped subagents.
- **Ground decisions in tool results.** For each step, the agent should assess
  progress from provider responses, draft state, and policy data.
- **Use checkpoints for blockers.** A blocker is not a reason to guess. It is a
  reason to ask the employee for a booking id, date, amount, route, hotel name,
  refund status, or card detail.
- **Restrict powerful tools.** Read-only evidence tools and write tools must not
  be available to the same unrestricted subagent.
- **Design tools for evals.** Tool names, input schemas, response envelopes, and
  error codes must be stable enough for programmatic grading.
- **Log a replayable trajectory.** Every external evidence call must have a
  normalized request, normalized response, provider mode, fixture id if mock,
  selected candidate, and reconciliation result.

---

## Canonical Subagents

The current code already emits subagent names in tool events. Keep these names
as the canonical eval surface. The canonical subagent specs live in:

```text
docs/subagents/receipt-reader.md
docs/subagents/evidence-reconciler.md
docs/subagents/draft-writer.md
```

Each spec follows the same frontmatter shape:

```yaml
---
name: receipt-reader
description: Use this subagent to ...
tools:
  - extract_receipt_fields
---
```

| Subagent | Conceptual role | Can read external evidence? | Can write draft? |
|---|---|---:|---:|
| [`receipt-reader`](subagents/receipt-reader.md) | OCR and claim extraction | No | No |
| [`evidence-reconciler`](subagents/evidence-reconciler.md) | Ctrip/Didi/card lookup and cross-check | Yes | No |
| [`draft-writer`](subagents/draft-writer.md) | Safe draft update or clarification | No | Yes, gated |

Do not add a fourth subagent until a concrete eval shows the current split is
too coarse. The important boundary is not the number of agents; it is that only
`draft-writer` can mutate the draft.

---

## Tool Catalog

### Existing Tools

| Tool | Owner subagent | Type | Purpose |
|---|---|---|---|
| `extract_receipt_fields` | `receipt-reader` | OCR/read | Extract merchant, amount, date, invoice number, tax, and confidence from the current draft receipt. |
| `detect_document_prompt_injection` | `receipt-reader` | safety/read | Detect malicious instructions embedded in uploaded receipts or documents. |
| `lookup_ctrip_booking` | `evidence-reconciler` | external evidence/read | Query Ctrip-style booking evidence for hotel, flight, train, or travel package claims. |
| `lookup_didi_trip` | `evidence-reconciler` | external evidence/read | Query Didi-style ride evidence for taxi and ride-hailing claims. |
| `lookup_card_transaction` | `evidence-reconciler` | external evidence/read | Query card transaction evidence for payment verification. |
| `check_duplicate_invoice` | `evidence-reconciler` | internal evidence/read | Check whether invoice/order/transaction identifiers were already reimbursed. |
| `get_policy_rules` | `evidence-reconciler` | policy/read | Fetch reimbursement policy, city tier, limits, tolerance, and payment rules. |
| `suggest_category` | `evidence-reconciler` | classifier/read | Suggest reimbursement category from normalized claim and evidence. |
| `update_draft_field` | `draft-writer` | mutation/write | Write a single draft field with an evidence source. |

### Logical Steps That Are Not Provider Tools

These can be implemented as pure functions or structured prompt steps. They
should not be confused with external evidence calls.

| Logical step | Owner | Purpose |
|---|---|---|
| `parse_user_claim` | `receipt-reader` | Extract dates, amounts, city, route, booking hints, and user intent from the employee's message. |
| `reconcile_evidence` | `evidence-reconciler` | Convert claim + provider results into a deterministic decision. |
| `ask_user_clarification` | `draft-writer` | Emit a final assistant message asking for specific missing or conflicting facts. |
| `record_external_api_trace` | tool wrapper | Persist trace for every Ctrip, Didi, and card provider call. |

---

## Shared Data Contracts

### ClaimFrame

`receipt-reader` outputs a normalized claim. It must preserve confidence and
missingness instead of filling gaps creatively.

```json
{
  "expense_type": "hotel",
  "amount": {"value": 680.0, "currency": "CNY", "confidence": "low", "source": "user_text"},
  "date": {"value": "2026-05-09", "confidence": "medium", "source": "user_text"},
  "merchant": {"value": "深圳酒店", "confidence": "low", "source": "ocr"},
  "city": {"value": "深圳", "confidence": "medium", "source": "user_text"},
  "route": null,
  "invoice_no": null,
  "booking_reference": null,
  "card_last4": null,
  "missing_fields": ["merchant_exact", "invoice_no"],
  "signals": ["blurry_receipt", "user_mentions_ctrip"]
}
```

### Provider Envelope

All external providers return one envelope shape.

```json
{
  "status": "ok",
  "provider_domain": "ctrip",
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

Allowed `status` values:

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

Provider errors and `not_configured` are not `not_found`. The assistant must
never tell the employee "no order was found" when the provider was unavailable
or not connected.

### EvidenceBundle

`evidence-reconciler` returns an evidence bundle. It can include raw provider
summaries, but all large payloads should live in trace rows.

```json
{
  "claim_id": "claim_001",
  "evidence_refs": ["trace_ctrip_001", "trace_card_001"],
  "ctrip": {"status": "ok", "candidate_count": 1},
  "didi": {"status": "not_applicable"},
  "card": {"status": "ok", "candidate_count": 1},
  "duplicate": {"matched_existing": false},
  "policy": {"category": "accommodation", "tolerance_amount": 1.0}
}
```

### ReconciliationDecision

The reconciliation decision is the only gate that authorizes draft writes.

```json
{
  "status": "can_write_draft",
  "reason": "Ctrip hotel booking and posted card transaction match date and net amount.",
  "labels": ["ctrip_card_match"],
  "verified_amount": 680.0,
  "currency": "CNY",
  "category": "accommodation",
  "fields_allowed": ["merchant", "amount", "date", "category", "description"],
  "fields_blocked": [],
  "clarification_questions": [],
  "evidence_refs": ["trace_ctrip_001", "trace_card_001"]
}
```

Allowed `status` values:

```text
can_write_draft
needs_user_clarification
blocked_write
provider_unavailable
read_only_blocked
```

`draft-writer` may call `update_draft_field` only for
`status = can_write_draft`.

---

## Orchestrator Flow

```text
user message + draft context
  -> receipt-reader
       extract_receipt_fields if receipt exists or user asks OCR/receipt help
       parse_user_claim from user text + OCR result
  -> route by claim signals
       hotel/flight/train/package -> Ctrip path
       taxi/ride-hailing -> Didi path
       card-only clue -> Card path
       unknown/missing core facts -> clarification
  -> evidence-reconciler
       call provider tools with normalized query
       trace every provider call
       apply deterministic reconciliation
  -> draft-writer
       if can_write_draft: update fields with evidence sources
       otherwise: ask concrete clarification or explain blocker
```

External evidence calls are bounded. Within one agent turn, count calls to:

```text
lookup_ctrip_booking
lookup_didi_trip
lookup_card_transaction
```

If the count reaches 5 and there is no safe write or blocking decision, end the
turn with:

```text
message_end.stop_reason = "needs_user_clarification"
```

The next employee reply starts a new turn and resets the counter.

---

## Path Logic

### Path 1: Ctrip + Card

Use this path when the claim is hotel, flight, train, or travel package, or
when the employee mentions Ctrip/Trip.com/booking/order.

Tool sequence:

```text
receipt-reader:
  extract_receipt_fields?
  parse_user_claim

evidence-reconciler:
  lookup_ctrip_booking(employee_id, date_range, amount_hint, city_hint, merchant_hint, booking_reference?)
  lookup_card_transaction(employee_id, date_range +/- 3 days, amount_hint, merchant_hint, card_last4?)
  check_duplicate_invoice(invoice_no?, booking_reference?, transaction_id?)
  get_policy_rules(category, city?, employee_level?)
  suggest_category(claim, evidence)

draft-writer:
  update_draft_field(...) only if can_write_draft
```

Decision rules:

| Evidence state | Decision |
|---|---|
| One completed Ctrip booking + one posted card transaction + amount/date/city match | `can_write_draft` |
| Ctrip booking exists but card transaction missing | `needs_user_clarification`, unless policy explicitly allows booking-only draft completion |
| Card transaction exists but Ctrip booking missing | `needs_user_clarification`; ask for order id, hotel name, or city/date confirmation |
| Ctrip amount differs from card or OCR beyond tolerance | `needs_user_clarification` or `blocked_write` if overclaim is clear |
| Booking canceled, no-show, fully refunded, or chargeback net amount is zero | `blocked_write` |
| Partial refund changes net amount | write only verified net amount, or ask user if claim amount does not equal net amount |
| Multiple same-day/same-amount bookings | `needs_user_clarification`; ask user to select the order |
| Provider timeout/error/not configured | `provider_unavailable`; do not say no booking exists |
| Duplicate invoice/order/transaction already reimbursed | `blocked_write` |

### Path 2: Didi + Card

Use this path when the claim is taxi, ride-hailing, airport transfer, commute,
or the employee mentions Didi.

Tool sequence:

```text
receipt-reader:
  extract_receipt_fields?
  parse_user_claim

evidence-reconciler:
  lookup_didi_trip(employee_id, date_range, amount_hint, city_or_route_hint, invoice_no?)
  lookup_card_transaction(employee_id, date_range +/- 3 days, amount_hint, merchant_hint="DIDI", card_last4?)
  check_duplicate_invoice(invoice_no?, didi_trip_id?, transaction_id?)
  get_policy_rules(category="transport", city?, employee_level?)
  suggest_category(claim, evidence)

draft-writer:
  update_draft_field(...) only if can_write_draft
```

Decision rules:

| Evidence state | Decision |
|---|---|
| One completed Didi trip + posted card/payment match + route/date/amount align | `can_write_draft` |
| Didi invoice is aggregated across multiple trips | `needs_user_clarification`; ask whether to split or attach all covered trips |
| Trip canceled, reassigned but not completed, or unpaid | `blocked_write` |
| Route city does not match business trip context | `needs_user_clarification` |
| Amount differs because of coupon, enterprise subsidy, tip, toll, parking, or wait fee | use verified `net_amount` only when provider explains the delta; otherwise ask |
| Invoice date is issue date, not ride date | use ride date as expense date and mention invoice issue date in description |
| Passenger account does not match employee | `needs_user_clarification` or `blocked_write` depending on policy |
| Duplicate invoice/trip already reimbursed | `blocked_write` |
| Provider timeout/error/not configured | `provider_unavailable`; do not say no trip exists |

### Path 3: Card Cross-Check

Use card evidence as a cross-check, not as a complete expense by itself unless
product policy explicitly allows card-only drafts.

Tool sequence:

```text
evidence-reconciler:
  lookup_card_transaction(employee_id, date_range, amount_hint, merchant_hint, card_last4?)
  check_duplicate_invoice(transaction_id?)
  get_policy_rules(category?, city?, employee_level?)
```

Decision rules:

| Evidence state | Decision |
|---|---|
| Posted card transaction matches OCR/user amount/date/merchant | strong supporting evidence |
| Pending authorization only | `needs_user_clarification`; ask user to wait for posting or provide receipt |
| Posted amount differs from booking/OCR | reconcile tax, service fee, exchange rate, or refund; otherwise ask |
| Refund, chargeback, reversal, or net amount zero | `blocked_write` |
| Merchant descriptor is vague | require Ctrip/Didi/OCR support before writing |
| Card owner differs from employee | `blocked_write` or manager approval path, depending on policy |
| Multiple card candidates | `needs_user_clarification` |
| Card provider unavailable | `provider_unavailable`; do not invent payment evidence |

---

## Failure Mode Matrix

Use this matrix to design mock provider fixtures and eval cases.

### Ctrip

| Failure mode | Mock status/label | Expected behavior |
|---|---|---|
| Receipt missing amount/date/merchant | `missing_fields` | Query Ctrip/card if user supplied enough hints; otherwise ask. |
| Blurry OCR merchant or amount | `blurry_receipt` signal | Prefer external evidence over low-confidence OCR, but preserve OCR trace. |
| Booking amount differs from invoice | `amount_mismatch` | Do not write amount until card/net amount explains the difference. |
| Booking canceled after purchase | `booking_cancelled` | Block write unless a non-refunded replacement booking is found. |
| Full refund | `full_refund` | Block write. |
| Partial refund | `partial_refund` | Use net amount or ask if claim amount differs. |
| Prepaid vs pay-at-hotel mismatch | `payment_channel_mismatch` | Cross-check card merchant and invoice issuer. |
| Multiple nights but one-day claim | `multi_night_partial_claim` | Ask whether claim is partial and what nights are being reimbursed. |
| Multiple rooms or guests | `multi_room_or_guest` | Ask which room/guest belongs to employee. |
| Employee not primary traveler/guest | `traveler_mismatch` | Ask for business relationship or block based on policy. |
| City matches but hotel name differs | `merchant_similarity_low` | Ask user to confirm hotel. |
| Invoice date differs from stay date | `invoice_issue_date_only` | Use stay/check-in date as expense date; retain invoice date in description. |
| Currency differs | `currency_mismatch` | Require FX conversion source. |
| Order during leave/weekend | `business_context_mismatch` | Ask for business purpose or route to review. |
| Duplicate booking/invoice already reimbursed | `duplicate_claim` | Block write. |
| Invoice unavailable | `invoice_unavailable` | Ask for alternate evidence or mark description as pending invoice. |
| Provider not connected | `not_configured` | Tell user verification source is not connected; do not say no order exists. |

### Didi

| Failure mode | Mock status/label | Expected behavior |
|---|---|---|
| Missing invoice | `missing_invoice` | Use trip + card evidence if unique; otherwise ask. |
| Aggregated invoice covers many trips | `aggregated_invoice` | Ask user to select or split covered trips. |
| Card amount differs from trip amount | `payment_delta` | Check coupon, enterprise subsidy, tip, toll, parking, wait fee. |
| Canceled or unfinished ride | `trip_not_completed` | Block write. |
| Reassigned driver / duplicate ride id | `trip_reassigned` | Use final completed trip only. |
| Pickup/dropoff not aligned with business context | `route_context_mismatch` | Ask for business purpose or route to review. |
| Ride time outside trip/work context | `time_context_mismatch` | Ask for clarification. |
| Multiple rides in one claim | `multi_trip_claim` | Ask whether to split or combine. |
| Same trip already reimbursed | `duplicate_trip` | Block write. |
| Personal account, enterprise payment, or mixed payment | `payment_owner_mismatch` | Ask or block based on policy. |
| Invoice date is issue date | `invoice_issue_date_only` | Use ride date as expense date. |
| Passenger differs from employee | `passenger_mismatch` | Ask or block based on policy. |
| Provider timeout/error/not configured | `provider_unavailable` | Explain provider issue, not not-found. |

### Credit Card

| Failure mode | Mock status/label | Expected behavior |
|---|---|---|
| Pending authorization only | `pending_only` | Do not treat as final payment; ask/wait. |
| Posted date differs from service date | `posting_lag` | Match with tolerance; use service date from booking/trip if available. |
| Hotel preauthorization differs from final charge | `preauth_final_delta` | Use posted final charge or ask. |
| Partial refund or reversal | `partial_refund` | Use net amount. |
| Full refund, chargeback, or reversal | `full_refund` | Block write. |
| Descriptor is vague | `descriptor_ambiguous` | Require Ctrip/Didi/OCR corroboration. |
| Multiple card transactions | `multiple_candidates` | Ask user to select transaction. |
| Split payment | `split_payment` | Sum only related posted transactions; ask if unsure. |
| One transaction covers multiple expenses | `compound_transaction` | Ask user to split. |
| Card last4 missing or not bound | `card_identity_missing` | Ask user to confirm card. |
| Card owner differs from employee | `card_owner_mismatch` | Block or require manager approval. |
| Currency/FX fee mismatch | `currency_mismatch` | Require exchange rate and fee treatment. |
| Duplicate imported transaction | `duplicate_transaction` | Block duplicate write. |
| Provider timeout/error/not configured | `provider_unavailable` | Explain provider issue, not not-found. |

---

## Trace Contract

Each Ctrip, Didi, and card tool call must write an external evidence trace.
The trace is the audit log and replay seed.

```json
{
  "trace_id": "trace_ctrip_001",
  "eval_run_id": "run_2026_05_17_001",
  "scenario_id": "hotel_blurry_receipt_ctrip_card_match",
  "conversation_turn_id": "turn_001",
  "draft_id": "draft_001",
  "employee_id": "emp_dev",
  "subagent": "evidence-reconciler",
  "tool_name": "lookup_ctrip_booking",
  "provider_domain": "ctrip",
  "provider_name": "ctrip_mock",
  "provider_mode": "mock",
  "fixture_id": "ctrip_hotel_shenzhen_680",
  "attempt_index": 1,
  "request_normalized": {
    "date_range": ["2026-05-09", "2026-05-09"],
    "amount_hint": 680.0,
    "city_hint": "深圳"
  },
  "response_normalized": {
    "status": "ok",
    "candidate_count": 1
  },
  "selected_candidate_id": "CTR-MOCK-001",
  "reconciliation_status": "can_write_draft",
  "reconciliation_labels": ["ctrip_card_match"],
  "fields_written": ["merchant", "amount", "date", "category"],
  "latency_ms": 12,
  "error": null,
  "created_at": "2026-05-17T00:00:00Z"
}
```

Trace assertions for eval:

```text
Every external evidence tool call has one trace row.
Trace rows include request_normalized and response_normalized.
Trace rows include provider_mode and fixture_id for mock/replay.
The selected candidate id matches the field source written to the draft.
Provider errors are represented as errors, not empty successful responses.
```

---

## Eval Manifest Pattern

Use YAML cases as tasks. Each run of a case is a trial. The full SSE event list,
tool calls, draft fields, and trace rows are the trajectory.

```yaml
- id: hotel_blurry_receipt_ctrip_card_match
  suite: receipt_completion_regression
  scenario: receipt_completion
  difficulty: regression
  tags: [hotel, ctrip, card, blurry_receipt]
  messages:
    - role: user
      content: "深圳酒店发票拍糊了，携程订单是5月9日680元，帮我补齐住宿报销。"
  initial_draft:
    missing_fields: [merchant, amount, date, category]
    receipt_confidence:
      amount: low
      merchant: low
  mock_providers:
    ctrip: ctrip_hotel_shenzhen_680
    card: card_shenzhen_hotel_680
  expect:
    required_subagents: [receipt-reader, evidence-reconciler, draft-writer]
    must_call_tools: [lookup_ctrip_booking, lookup_card_transaction, update_draft_field]
    forbidden_tools: [submit_report, approve_report, pay_report]
    agent_trace_present: true
    external_trace_count_min: 2
    reconciliation_status: can_write_draft
    final_fields:
      merchant: "深圳南山商务酒店"
      amount: 680
      date: "2026-05-09"
      category: accommodation
    field_sources_include:
      amount: ctrip_card_match
```

### Required Graders

| Grader | Purpose |
|---|---|
| `must_call_tools` | Prevents the agent from guessing without evidence. |
| `forbidden_tools_absent` | Prevents submit/approve/pay or unauthorized writes. |
| `required_subagents` | Ensures the intended workflow path happened. |
| `agent_trace_present` | Ensures aggregate trajectory is emitted. |
| `external_trace_shape` | Ensures each provider call has request/response/mode/fixture. |
| `reconciliation_status` | Ensures conflicts produce ask/block, not writes. |
| `final_fields` | Verifies draft field values after safe write. |
| `field_sources_include` | Verifies fields cite evidence source. |
| `response_contains` / `response_excludes` | Verifies user-facing explanation and avoids false not-found language. |
| `stop_reason` | Verifies 5-attempt unresolved cases stop with `needs_user_clarification`. |

---

## Starter Eval Matrix

Regression cases should be stable and must-pass. Capability cases should be
harder and can intentionally expose current weaknesses.

| Case id | Providers | Expected decision | Must-call tools |
|---|---|---|---|
| `hotel_blurry_receipt_ctrip_card_match` | Ctrip ok, card ok | `can_write_draft` | Ctrip, card, write |
| `hotel_ocr_amount_differs_from_booking` | OCR 780, Ctrip/card 680 | `needs_user_clarification` or write 680 only if user confirms | Ctrip, card |
| `hotel_booking_cancelled_after_invoice` | Ctrip canceled/refunded, card refunded | `blocked_write` | Ctrip, card, duplicate |
| `hotel_multiple_same_day_candidates` | Ctrip multiple, card one | `needs_user_clarification` | Ctrip, card |
| `hotel_card_provider_timeout` | Ctrip ok, card timeout | `provider_unavailable` | Ctrip, card |
| `taxi_missing_invoice_didi_card_match` | Didi ok, card ok | `can_write_draft` | Didi, card, write |
| `taxi_aggregated_invoice_multi_trip` | Didi multiple trips, card compound | `needs_user_clarification` | Didi, card |
| `taxi_cancelled_trip_card_pending` | Didi canceled, card pending | `blocked_write` | Didi, card |
| `taxi_route_context_mismatch` | Didi route unrelated | `needs_user_clarification` | Didi, policy |
| `card_pending_only_no_receipt` | Card pending only | `needs_user_clarification` | Card |
| `card_full_refund_after_booking` | Card full refund | `blocked_write` | Card, Ctrip or Didi |
| `provider_not_configured_not_not_found` | Ctrip/card stub | `provider_unavailable` | Provider tool called |
| `five_attempts_unresolved_then_ask` | Repeated no safe candidate | `needs_user_clarification` | Exactly 5 external evidence calls |
| `duplicate_invoice_already_reimbursed` | Duplicate true | `blocked_write` | Duplicate check |

---

## Mock Provider Fixture Rules

Mock provider data is not fake product logic. It is the controlled external
environment for agent eval.

Fixture rules:

- Each fixture has a `fixture_id`.
- Each fixture declares its behavior: `ok`, `not_found`, `multiple_candidates`,
  `timeout`, `provider_error`, `not_configured`, or a domain label such as
  `partial_refund`.
- Eval fixtures must be deterministic. Do not use random failures in CI.
- Chaos behavior can exist locally, but it must be disabled for regression and
  capability evals.
- The agent must not read fixture manifests directly. Only provider tools can
  return fixture data.

Example:

```yaml
- fixture_id: ctrip_hotel_shenzhen_680
  provider_domain: ctrip
  behavior: ok
  candidates:
    - booking_id: CTR-MOCK-001
      booking_type: hotel
      status: completed
      employee_id: emp_dev
      check_in: 2026-05-09
      check_out: 2026-05-10
      city: 深圳
      hotel_name: 深圳南山商务酒店
      amount: 680.00
      refund_amount: 0.00
      net_amount: 680.00
      currency: CNY
      invoice_available: true

- fixture_id: card_shenzhen_hotel_680
  provider_domain: card
  behavior: ok
  candidates:
    - transaction_id: CARD-MOCK-001
      employee_id: emp_dev
      date: 2026-05-09
      merchant: SHENZHEN HOTEL
      amount: 680.00
      refund_amount: 0.00
      net_amount: 680.00
      currency: CNY
      status: posted
      card_last4: "1888"
```

---

## Implementation Checklist

Use this checklist when turning the design into code.

- [ ] Keep `SUBAGENT_TOOL_MAP` aligned with the tool catalog above.
- [ ] Ensure the system prompt states that only `draft-writer` may mutate draft
      fields.
- [ ] Normalize Ctrip, Didi, and card tool responses into the provider envelope.
- [ ] Add mock provider fixtures for the starter eval matrix.
- [ ] Persist one external trace row per provider call.
- [ ] Add grader support for external trace shape and reconciliation status.
- [ ] Add eval cases for Ctrip, Didi, card, provider-not-configured, and
      five-attempt unresolved paths.
- [ ] Keep OCR/LLM real or replay independently from Ctrip/Didi/card provider
      modes.
- [ ] Ensure `not_configured`, `timeout`, and `provider_error` never surface as
      "not found".
- [ ] Verify blocked and clarification cases do not call `update_draft_field`.

Recommended local/eval modes:

```text
AGENT_LLM_PROVIDER=real
OCR_PROVIDER=real|replay
CTRIP_PROVIDER=mock
DIDI_PROVIDER=mock
CARD_PROVIDER=mock
TRACE_SINK=sqlite
```

---

## References

- Anthropic, [Agents for financial services](https://www.anthropic.com/news/finance-agents):
  governed connectors and MCP apps for financial data access.
- Anthropic Engineering, [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents):
  simple composable patterns, tool-grounded progress, human checkpoints, and
  stopping conditions.
- Anthropic Engineering, [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents):
  clear tool definitions, judicious context, and eval-driven tool iteration.
- Anthropic Engineering, [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  tasks, trials, trajectories, and graders for multi-turn agents.
- Anthropic Docs, [Create custom subagents](https://code.claude.com/docs/en/sub-agents):
  isolated subagent contexts and tool access controls.
- ExpenseFlow, [External Evidence Provider Pattern](external-api-provider-pattern.md):
  provider modes, normalized candidate schema, reconciliation contract, and
  five-attempt clarification guard.
