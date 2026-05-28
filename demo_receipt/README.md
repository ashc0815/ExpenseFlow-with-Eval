# Expense Reconciliation Eval Dataset

This folder contains receipt images plus deterministic eval fixtures for the
employee expense assistant external-evidence path.

The dataset is designed to test two things:

1. Flexibility: how the agent handles missing fields, provider errors,
   conflicting data, cancellations, refunds, rebookings, and permission issues.
2. Traceability: whether every external evidence call can be replayed from a
   normalized request/response plus provider mode and fixture id.

## Files

- `expense_reconciliation_eval_cases.yaml` — 45 eval cases across golden path,
  missing fields, conflicts, lifecycle, safety, and trace/platform scenarios.
- `mock_ctrip_bookings.yaml` — deterministic Ctrip/Trip.com-style booking
  fixtures.
- `mock_didi_trips.yaml` — deterministic Didi-style ride fixtures.
- `mock_card_transactions.yaml` — deterministic card transaction fixtures.
- `receipt_ocr_overrides.yaml` — receipt image OCR expectations and optional
  replay records for deterministic reconciliation evals.

## How To Use

Run evals with real OCR/tool-calling if you are testing the live extraction
path. For reconciliation and trace evals, use `receipt_ocr_overrides.yaml` as
record/replay input so the only changing variable is the agent behavior.

External provider tools should not read `expense_reconciliation_eval_cases.yaml`
directly. The eval harness selects a case, configures provider fixtures, then
the agent receives only normal tool responses from:

- `lookup_ctrip_booking`
- `lookup_didi_trip`
- `lookup_card_transaction`

Each provider response should include at least:

```json
{
  "status": "ok",
  "provider_mode": "mock",
  "fixture_id": "ctrip_gold_hotel_shenzhen_680",
  "request_normalized": {},
  "response_normalized": {},
  "candidates": [],
  "candidate_count": 0,
  "error": null
}
```

## Important Fixture Note

The existing local images are useful for OCR smoke tests, but several are
generic retail or restaurant receipts rather than Ctrip/Didi domain receipts.
For deterministic external-evidence evals, prefer the replay OCR records in
`receipt_ocr_overrides.yaml`. When you later add real Ctrip/Didi/card invoices,
replace the corresponding `receipt.image` values without changing case ids.

