---
name: external-evidence-reconciliation
description: Cross-check Ctrip, Didi, card, duplicate, and policy evidence to produce a safe reimbursement decision.
used_by:
  - evidence-reconciler
connectors:
  - ctrip-evidence-provider
  - didi-evidence-provider
  - card-transaction-provider
  - duplicate-expense-ledger
  - expense-policy-store
tools:
  - lookup_ctrip_booking
  - lookup_didi_trip
  - lookup_card_transaction
  - check_duplicate_invoice
  - get_policy_rules
---

# external-evidence-reconciliation

## Procedure

1. Choose the path from `ClaimFrame` signals:
   Ctrip + card, Didi + card, card cross-check, or clarification.
2. Query only the providers needed for the path.
3. Compare date, amount, net amount, currency, merchant similarity, city/route,
   employee ownership, refund/cancel status, and duplicates.
4. Classify the outcome as `can_write_draft`, `needs_user_clarification`,
   `blocked_write`, `provider_unavailable`, or `read_only_blocked`.
5. Return an `EvidenceBundle` and `ReconciliationDecision`.

## Guardrails

- Provider errors are not evidence of absence.
- Multiple candidates require user confirmation unless a unique id matches.
- Canceled, fully refunded, chargeback, duplicate, unfinished, or owner-mismatch
  evidence blocks writes.
- Partial refunds can authorize only the verified net amount.
- Card evidence alone is supporting evidence unless policy allows card-only
  completion.
- Stop after five unresolved external evidence calls in one turn.
