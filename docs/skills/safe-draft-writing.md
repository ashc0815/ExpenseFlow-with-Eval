---
name: safe-draft-writing
description: Write reimbursement draft fields only when reconciliation explicitly authorizes the write.
used_by:
  - draft-writer
connectors:
  - draft-store
tools:
  - update_draft_field
---

# safe-draft-writing

## Procedure

1. Read the `ReconciliationDecision`.
2. If status is `can_write_draft`, write only fields listed in
   `fields_allowed`.
3. Add a source to every field write.
4. If status is anything else, do not call write tools.
5. Return a concise summary, clarification question, or blocker explanation.

## Guardrails

- Never write unverified amounts.
- Never write fields for provider failures, conflicts, duplicates,
  cancellations, full refunds, chargebacks, or unresolved multi-candidate cases.
- Never submit, approve, reject, or pay.
