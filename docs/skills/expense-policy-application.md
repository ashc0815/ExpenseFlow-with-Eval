---
name: expense-policy-application
description: Apply policy limits, tolerance, category rules, invoice requirements, and approval context to reimbursement decisions.
used_by:
  - evidence-reconciler
connectors:
  - expense-policy-store
tools:
  - get_policy_rules
  - suggest_category
---

# expense-policy-application

## Procedure

1. Load policy rules when category, city tier, employee level, invoice
   requirement, payment method, tolerance, or approval path matters.
2. Use policy tolerance to judge small amount deltas.
3. Use category rules to distinguish accommodation, transport, meal,
   entertainment, and other claims.
4. Return policy facts as supporting evidence, not free-form assumptions.

## Guardrails

- Do not invent policy values.
- If the policy store is unavailable, ask or route to manual review.
- Do not use policy tolerance to approve clear overclaims, refunds, duplicates,
  or unsupported card-only claims.
