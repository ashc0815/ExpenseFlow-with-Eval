---
name: provider-traceability
description: Ensure each connector-backed tool call has replayable request/response trace metadata for audit and eval.
used_by:
  - receipt-reader
  - evidence-reconciler
  - draft-writer
connectors:
  - trace-sink
tools:
  - extract_receipt_fields
  - lookup_ctrip_booking
  - lookup_didi_trip
  - lookup_card_transaction
  - update_draft_field
---

# provider-traceability

## Procedure

1. Assign each agent turn a conversation turn id.
2. For every connector-backed tool call, record normalized request and response.
3. Include subagent, skill, tool, connector, provider mode, fixture id, candidate
   count, selected candidate id, reconciliation status, fields written, latency,
   and error.
4. Make mock/replay traces deterministic enough for eval reproduction.

## Guardrails

- Do not log raw credentials or unnecessary PII.
- Do not drop failed provider calls.
- Do not collapse provider errors into empty successful responses.
- Eval must fail when a connector-backed tool call lacks trace metadata.
