# External Evidence Provider Pattern

> **Status:** Pattern doc for the employee expense assistant.
>
> This document defines how ExpenseFlow should integrate external evidence
> providers for receipt completion, especially Ctrip/Trip.com bookings and
> corporate-card transactions. It deliberately focuses on the employee
> reimbursement assistant flow, not the fraud/OODA investigator.

---

## TL;DR

The employee assistant can use external evidence to complete a draft only
when the evidence is unique, normalized, and reconciled.

The core contract is:

1. Providers are swappable: `mock`, `sandbox`, `real`, or `stub`.
2. Mock fixtures are first-class and drive evals.
3. Provider responses normalize into one candidate schema.
4. Ctrip booking evidence must be reconciled against card evidence before
   the assistant writes fields that affect reimbursement.
5. If evidence remains uncertain after 5 external-evidence tool calls in the
   same turn, the agent stops tool use, asks the user to confirm missing
   information, and waits for the next user reply.
6. Every provider call is traceable enough to replay the decision.

The agent should never infer a reimbursable amount from vibes. Evidence first,
clarification second, write last.

---

## Scope

This pattern covers these employee-facing tools:

```text
lookup_ctrip_booking
lookup_card_transaction
lookup_didi_trip
update_draft_field
```

For the subagent ownership, tool-call routing, failure-mode matrix, trace
assertions, and eval manifest shape, see
[`expense-assistant-subagent-tool-calls.md`](expense-assistant-subagent-tool-calls.md).

The examples below focus on the Ctrip + card path:

```text
user says: "Ctrip hotel order, May 9, Shenzhen, 680 CNY"
  -> lookup_ctrip_booking(...)
  -> lookup_card_transaction(...)
  -> reconcile booking status, refund, net amount, card charge
  -> update_draft_field(...) only if safe
```

This pattern does not cover approval, rejection, payment, or fraud
investigation tools. Those may reuse provider infrastructure, but they are not
the acceptance target for this document.

---

## Provider Shape

Each external domain gets a small provider interface plus mock/sandbox/real
implementations. The agent tool calls the interface, not a vendor SDK.

```python
class CtripBookingProvider(Protocol):
    async def lookup_booking(self, query: CtripBookingQuery) -> EvidenceLookupResult:
        ...


class CardTransactionProvider(Protocol):
    async def lookup_transaction(self, query: CardTransactionQuery) -> EvidenceLookupResult:
        ...
```

Provider selection is environment-driven:

```text
CTRIP_PROVIDER=mock|sandbox|real|stub
CARD_PROVIDER=mock|sandbox|real|stub
```

Rules:

- `mock` is the default for local dev, demo, CI, and eval.
- `sandbox` uses a vendor or MCP sandbox and must normalize into the same
  schema as mock.
- `real` is production integration and must have trace logging enabled.
- `stub` returns a structured `provider_not_configured` result. It should not
  pretend no order exists.

Do not let a missing integration surface as "no matching order." That is a
different business fact. Missing provider means "cannot verify with this
provider yet."

---

## Normalized Candidate Schema

All Ctrip and card providers return the same top-level result shape:

```json
{
  "source": "ctrip_mock",
  "provider": "mock",
  "status": "ok",
  "query": {},
  "candidates": [],
  "confidence": 0.0,
  "error": null
}
```

`status` values:

```text
ok
not_configured
provider_error
timeout
malformed_response
auth_error
rate_limited
```

Candidate fields for Ctrip:

```json
{
  "booking_id": "ctrip-hotel-001",
  "booking_type": "hotel",
  "status": "active",
  "date": "2026-05-09",
  "merchant": "Ctrip",
  "vendor": "Shenzhen Nanshan Business Hotel",
  "amount": 680.0,
  "refund_amount": 0.0,
  "net_amount": 680.0,
  "currency": "CNY",
  "city": "Shenzhen",
  "nights": 1,
  "hotel": "Shenzhen Nanshan Business Hotel",
  "invoice_status": "issued",
  "invoice_available": true
}
```

Candidate fields for card:

```json
{
  "transaction_id": "card-003",
  "date": "2026-05-09",
  "merchant": "SHENZHEN HOTEL",
  "amount": 680.0,
  "refund_amount": 0.0,
  "net_amount": 680.0,
  "currency": "CNY",
  "card_last4": "1888"
}
```

The normalized schema is the eval contract. Mock, sandbox, and real providers
must all return these fields where available.

---

## Reconciliation Contract

The assistant can write draft fields only after reconciliation returns a safe
decision.

```python
class ReconciliationDecision(TypedDict):
    status: Literal["can_write_draft", "blocked_write", "needs_user_clarification"]
    reason: str
    labels: list[str]
    claim_amount: float | None
    verified_amount: float | None
    category: Literal["accommodation", "transport", "meal", "other"] | None
```

Decision rules:

- One Ctrip candidate + one matching card transaction:
  `can_write_draft`, label `ctrip_card_match`.
- One Ctrip candidate + no unique card match:
  can write only low-risk descriptive fields if product policy allows it;
  otherwise `needs_user_clarification`.
- Cancelled booking with `net_amount <= 0`:
  `blocked_write`, label `booking_cancelled/full_refund`.
- Claimed amount greater than verified net amount by tolerance:
  `blocked_write`, label `over_claim_risk`.
- Multiple Ctrip or card candidates:
  `needs_user_clarification`.
- Provider not configured or provider error:
  `needs_user_clarification`; do not say "no order found."
- No candidate:
  `needs_user_clarification` unless the user explicitly asks a policy-only
  question.

Fields that can be written after `can_write_draft`:

```text
merchant
amount
date
category
description
```

Each write must include a source such as:

```text
ctrip_card_match
ctrip_booking_only
ctrip_booking_partial_claim
card_transaction_match
```

---

## Five-Attempt Clarification Guard

The employee assistant must not loop forever trying slightly different external
evidence queries.

Within a single agent turn, count calls to:

```text
lookup_ctrip_booking
lookup_card_transaction
lookup_didi_trip
```

If the count reaches 5 and the agent has not safely progressed to a draft
update or a blocking decision, the turn must end with:

```text
message_end.stop_reason = "needs_user_clarification"
```

The assistant should ask for concrete missing facts:

```text
order id
date
amount
city / hotel / flight / route / merchant keyword
refund, cancellation, or rebooking status
```

When the user replies, that reply starts a new turn and the attempt counter
resets. The agent may then call the provider tools again with the confirmed
information.

This guard is not a substitute for provider integration. It is a safety rail
for both mock and real providers.

---

## Draft Context Gate

External evidence completion is a draft-writing flow. If the request arrives
without `draft_id`, the assistant should not attempt to write fields.

Expected behavior:

- With `draft_id`: external evidence tools may be used, then
  `update_draft_field` may run if reconciliation is safe.
- Without `draft_id`: answer policy/read-only questions, or ask the user to
  open/create a draft before completion.
- On a report detail page for a rejected report: guide the user to "re-edit"
  or create a new draft before trying to complete missing evidence.

This avoids the misleading failure mode where the assistant says it cannot
find an order when the real problem is that there is no writable draft context.

---

## Trace Schema

Every provider call should be replayable. A trace row should answer:

- What tool was called?
- Which provider mode handled it?
- What normalized request was sent?
- What normalized candidates came back?
- Was this the first or fifth attempt in the turn?
- Did the call contribute to a write, a block, or a clarification?

Recommended table:

```sql
CREATE TABLE external_api_trace (
    id                       TEXT PRIMARY KEY,
    trace_id                 TEXT NOT NULL,
    conversation_turn_id     TEXT,
    draft_id                 TEXT,
    report_id                TEXT,
    user_id                  TEXT,

    tool_name                TEXT NOT NULL,
    provider_domain          TEXT NOT NULL, -- ctrip_booking / card_transaction
    provider_name            TEXT NOT NULL, -- MockCtripProvider / StripeCardProvider
    provider_mode            TEXT NOT NULL, -- mock / sandbox / real / stub
    operation                TEXT NOT NULL, -- lookup_booking / lookup_transaction
    attempt_index            INTEGER NOT NULL,

    request_normalized       JSON NOT NULL,
    response_normalized      JSON,
    candidate_count          INTEGER NOT NULL DEFAULT 0,
    selected_candidate_id    TEXT,

    reconciliation_status    TEXT, -- can_write_draft / blocked_write / needs_user_clarification
    reconciliation_labels    JSON,
    clarification_required   BOOLEAN NOT NULL DEFAULT FALSE,
    fields_written           JSON,

    latency_ms               INTEGER,
    error                    TEXT,
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_external_trace_trace_id ON external_api_trace(trace_id);
CREATE INDEX idx_external_trace_turn ON external_api_trace(conversation_turn_id);
CREATE INDEX idx_external_trace_draft ON external_api_trace(draft_id);
CREATE INDEX idx_external_trace_provider ON external_api_trace(provider_domain, provider_mode, created_at);
CREATE INDEX idx_external_trace_error ON external_api_trace(error) WHERE error IS NOT NULL;
```

Minimum trace payloads for eval replay:

```json
{
  "tool_name": "lookup_ctrip_booking",
  "provider_mode": "mock",
  "attempt_index": 3,
  "request_normalized": {"date": "2026-05-09", "amount": 680, "merchant_hint": "Shenzhen"},
  "response_normalized": {"status": "ok", "candidates": []},
  "candidate_count": 0,
  "clarification_required": false
}
```

---

## Eval Contract

The eval suite should test the assistant behavior, not just provider behavior.

Required assertions:

```text
must_call_tools
forbidden_tools_absent
response_contains
final_fields
message_end.stop_reason
```

Required cases:

```yaml
- id: ctrip_card_unique_match_writes_draft
  user: "Shenzhen hotel receipt is blurry. Ctrip order May 9, 680 CNY."
  context: {draft_id: draft-1}
  fixtures:
    ctrip_provider: ctrip_hotel_680.yaml
    card_provider: card_hotel_680.yaml
  must_call_tools: [lookup_ctrip_booking, lookup_card_transaction, update_draft_field]
  final_fields:
    merchant: "Shenzhen Nanshan Business Hotel"
    amount: 680
    category: accommodation

- id: ctrip_over_claim_blocks_write
  user: "Ctrip hotel order May 9 Shenzhen 900 CNY, complete this."
  context: {draft_id: draft-1}
  fixtures:
    ctrip_provider: ctrip_hotel_680.yaml
    card_provider: card_hotel_680.yaml
  must_call_tools: [lookup_ctrip_booking, lookup_card_transaction]
  forbidden_tools_absent: [update_draft_field]
  response_contains: ["higher than verified net amount"]

- id: ctrip_multiple_candidates_asks_user
  user: "Ctrip hotel May 9 680 CNY."
  context: {draft_id: draft-1}
  fixtures:
    ctrip_provider: ctrip_two_hotels_same_amount.yaml
  must_call_tools: [lookup_ctrip_booking]
  forbidden_tools_absent: [update_draft_field]
  response_contains: ["confirm", "order"]

- id: ctrip_provider_not_configured_does_not_claim_no_order
  user: "Ctrip order May 9 680 CNY, complete this."
  context: {draft_id: draft-1}
  fixtures:
    ctrip_provider: stub_not_configured.yaml
  must_call_tools: [lookup_ctrip_booking]
  forbidden_tools_absent: [update_draft_field]
  response_contains: ["provider", "not configured"]
  response_must_not_contain: ["no order found"]

- id: external_evidence_five_attempts_needs_clarification
  user: "Ctrip hotel order amount is unclear, complete this."
  context: {draft_id: draft-1}
  fixtures:
    ctrip_provider: always_empty.yaml
  must_call_tools:
    - lookup_ctrip_booking
  expected_tool_call_count: 5
  forbidden_tools_absent: [update_draft_field]
  message_end:
    stop_reason: needs_user_clarification
  response_contains: ["5", "confirm"]

- id: user_clarifies_then_agent_continues
  turns:
    - user: "Ctrip hotel order amount is unclear, complete this."
      expected_stop_reason: needs_user_clarification
    - user: "Order id is ctrip-hotel-001, May 9, Shenzhen, 680 CNY, no refund."
      must_call_tools: [lookup_ctrip_booking, lookup_card_transaction, update_draft_field]
  final_fields:
    amount: 680
    category: accommodation
```

Mock fixtures must include:

- unique Ctrip + unique card match
- Ctrip unique but card missing
- card unique but Ctrip missing
- multiple Ctrip candidates
- multiple card candidates
- cancelled / full refund
- partial refund
- provider not configured
- provider timeout / 500
- malformed provider response
- prompt injection string inside vendor metadata

---

## Provider Checklist

Before merging a new external evidence provider:

- [ ] Interface returns normalized lookup results.
- [ ] Mock provider exists and is the default.
- [ ] Fixtures cover success, empty, multiple, refund, error, malformed, and injection-like metadata.
- [ ] Sandbox/real provider normalizes into the same schema.
- [ ] Missing provider returns `not_configured`, not "no match."
- [ ] Tool handler does not leak vendor SDK details to the agent.
- [ ] Trace row includes provider mode, attempt index, request, response, candidate count, and reconciliation outcome.
- [ ] Evals assert no write on uncertain evidence.
- [ ] Evals assert 5 attempts -> `needs_user_clarification`.
- [ ] Evals assert user clarification lets the next turn continue.

---

## Current Implementation Notes

As of this pattern update, the code already has deterministic local mock
fixtures inside the employee chat route for:

```text
lookup_ctrip_booking
lookup_card_transaction
lookup_didi_trip
```

The Ctrip/card mock path includes a known happy path:

```text
date: 2026-05-09
city: Shenzhen
amount: 680 CNY
category: accommodation
```

The assistant also has a hard guard for 5 external-evidence tool attempts in
one turn. If still unresolved, it pauses for user clarification rather than
continuing to tool-call or guessing.

Future work should move inline mock data into provider modules and fixtures,
then add `external_api_trace` persistence around both mock and real providers.
