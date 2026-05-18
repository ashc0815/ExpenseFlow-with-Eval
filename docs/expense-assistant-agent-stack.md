# Expense Assistant Agent Stack

> **Status:** Canonical relationship model for the employee reimbursement
> assistant.
>
> This document maps ExpenseFlow to the same operating model used by Anthropic's
> financial-services examples: one orchestrating agent, reusable skills,
> connector-backed tools, and narrowly scoped subagents.

---

## Pattern Borrowed

The Anthropic financial-services repo separates responsibilities into three
layers:

1. **Agent / plugin manifest**: the runnable persona and orchestration boundary.
2. **Skills**: reusable domain procedures the agent can apply across tools.
3. **Connectors / MCP servers**: governed data access points, such as financial
   filings, market data, spreadsheets, internal records, or write targets.

ExpenseFlow should use the same separation:

```text
expense-assistant agent
  -> loads skills
  -> delegates to subagents
  -> exposes connectors only through allowed tools
  -> writes trace for every connector-backed call
```

Subagents are leaf workers. They do not own provider integration directly. A
subagent is allowed to call tools, tools call connectors, and connectors return
normalized evidence.

The GL Reconciler cookbook also provides the safety shape we want:

| Anthropic GL Reconciler role | Safety boundary | ExpenseFlow equivalent |
|---|---|---|
| `reader` | Reads untrusted counterparty files; no MCP servers; no write tools; structured output only. | `receipt-reader` reads untrusted receipts/OCR; no Ctrip/Didi/card/policy/draft connectors. |
| `critic` | Re-verifies against trusted GL/subledger MCPs; read-only. | `evidence-reconciler` verifies against Ctrip/Didi/card/policy/ledger connectors; read-only. |
| `resolver` | Only worker with write; receives verified breaks; never reads untrusted files. | `draft-writer` is the only draft mutation worker; receives `ReconciliationDecision`; no OCR/external evidence connectors. |

This is the most important design point: isolate untrusted receipt reading from
trusted connector reconciliation, and isolate both from the write-capable worker.

---

## Canonical Files

| Layer | ExpenseFlow file | Purpose |
|---|---|---|
| Agent | [`agents/expense-assistant.md`](agents/expense-assistant.md) | Top-level orchestrator manifest. |
| Subagents | [`subagents/receipt-reader.md`](subagents/receipt-reader.md) | OCR and claim extraction worker. |
| Subagents | [`subagents/evidence-reconciler.md`](subagents/evidence-reconciler.md) | Ctrip/Didi/card evidence worker. |
| Subagents | [`subagents/draft-writer.md`](subagents/draft-writer.md) | Safe draft mutation worker. |
| Skills | [`skills/receipt-claim-extraction.md`](skills/receipt-claim-extraction.md) | How to normalize OCR/user text into a claim. |
| Skills | [`skills/external-evidence-reconciliation.md`](skills/external-evidence-reconciliation.md) | How to cross-check Ctrip, Didi, and card evidence. |
| Skills | [`skills/expense-policy-application.md`](skills/expense-policy-application.md) | How policy limits and tolerances affect decisions. |
| Skills | [`skills/safe-draft-writing.md`](skills/safe-draft-writing.md) | How and when draft fields may be written. |
| Skills | [`skills/provider-traceability.md`](skills/provider-traceability.md) | How connector calls become replayable traces. |
| Connectors | [`connectors/expense-evidence-connectors.md`](connectors/expense-evidence-connectors.md) | Registry of OCR, Ctrip, Didi, card, policy, ledger, draft, and trace connectors. |

---

## Runtime Relationship

```text
employee message + draft context
  -> expense-assistant
       skills:
         receipt-claim-extraction
         external-evidence-reconciliation
         expense-policy-application
         safe-draft-writing
         provider-traceability
       callable subagents:
         receipt-reader
         evidence-reconciler
         draft-writer

receipt-reader
  -> tools:
       extract_receipt_fields
       (prompt-injection scan is middleware, not a tool)
  -> connectors through tools:
       ocr-provider
       trace-sink
  -> output:
       ClaimFrame

evidence-reconciler
  -> tools:
       lookup_ctrip_booking
       lookup_didi_trip
       lookup_card_transaction
       check_duplicate_invoice
       get_policy_rules
       suggest_category
  -> connectors through tools:
       ctrip-evidence-provider
       didi-evidence-provider
       card-transaction-provider
       duplicate-expense-ledger
       expense-policy-store
       trace-sink
  -> output:
       EvidenceBundle + ReconciliationDecision

draft-writer
  -> tools:
       update_draft_field
  -> connectors through tools:
       draft-store
       trace-sink
  -> output:
       draft updates or final clarification/blocker text
```

---

## Relationship Rules

- **Agents load skills.** Skills are domain instructions and reusable methods,
  not external data sources.
- **Subagents inherit only the skills they need.** `receipt-reader` should not
  receive Ctrip/card reconciliation instructions; `draft-writer` should not
  receive provider search instructions.
- **Tools are the only connector interface.** A subagent never reads fixtures,
  raw provider files, sandbox payloads, or card data directly.
- **Connectors are mode-swappable.** OCR can be `real` while Ctrip/Didi/card are
  `mock`; evals should also support `replay`, `stub`, and error modes.
- **Only `draft-writer` can mutate drafts.** Evidence tools are read-only even
  when their provider result is high-confidence.
- **Connector failure is not not-found.** `timeout`, `provider_error`,
  `auth_error`, `rate_limited`, and `not_configured` must surface as provider
  availability states.
- **Trace is mandatory.** Every connector-backed call must produce a trace row
  with normalized request, normalized response, provider mode, fixture id when
  applicable, and reconciliation status.
- **Leaf workers do not call each other.** The orchestrator routes handoffs,
  validates structured output, and then passes the validated payload to the next
  subagent.
- **Untrusted content has a narrow output schema.** Receipt/OCR results should
  become a `ClaimFrame`, not free-form instructions that later workers might
  accidentally follow.

---

## Eval Implications

The eval harness should grade all four layers:

| Layer | Eval assertion |
|---|---|
| Agent | Required subagents appeared in the trajectory. |
| Skill | The response followed the skill's decision rules, such as not writing on conflicts. |
| Tool | Required tool calls happened and forbidden tools were absent. |
| Connector | Each connector-backed tool call produced a replayable request/response trace. |

For example, a Ctrip hotel claim with a blurry receipt and matching card charge
should assert:

```yaml
expect:
  required_subagents: [receipt-reader, evidence-reconciler, draft-writer]
  required_skills:
    - receipt-claim-extraction
    - external-evidence-reconciliation
    - safe-draft-writing
    - provider-traceability
  must_call_tools:
    - lookup_ctrip_booking
    - lookup_card_transaction
    - update_draft_field
  required_connectors:
    - ctrip-evidence-provider
    - card-transaction-provider
    - draft-store
    - trace-sink
  reconciliation_status: can_write_draft
```

---

## Implementation Target

The current code already emits `subagent` on tool events. The next implementation
step is to make skills and connectors explicit in traces:

```json
{
  "subagent": "evidence-reconciler",
  "skills": ["external-evidence-reconciliation", "provider-traceability"],
  "tool_name": "lookup_ctrip_booking",
  "connector": "ctrip-evidence-provider",
  "provider_mode": "mock",
  "fixture_id": "ctrip_hotel_shenzhen_680",
  "request_normalized": {},
  "response_normalized": {},
  "reconciliation_status": "can_write_draft"
}
```

That makes eval failures easier to explain:

```text
wrong subagent
right subagent but wrong skill logic
right skill but wrong tool selection
right tool but bad connector response handling
right connector but missing trace
```

---

## References

- Anthropic financial-services repo: https://github.com/anthropics/financial-services
- GL Reconciler managed-agent cookbook:
  https://github.com/anthropics/financial-services/tree/main/managed-agent-cookbooks/gl-reconciler
- ExpenseFlow subagent tool-call design:
  [`expense-assistant-subagent-tool-calls.md`](expense-assistant-subagent-tool-calls.md)
