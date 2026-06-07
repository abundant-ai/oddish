---
name: reconciliation-output-schemas
description: Use this skill when writing finance reconciliation CSV, summary JSON, or adjustment timeline JSON outputs with stable schemas and ordering.
---

# Reconciliation Output Schemas

## `reconciled_payments.csv`

Write one row per outgoing payment. Sort by `payment_date` then `payment_id`.

Columns:

```text
payment_id,payment_date,invoice_id,vendor_id,vendor_name,amount,currency,match_status,days_late,risk_tier,exception_code
```

Use two-decimal positive strings for payment amounts. Use semicolon-separated sorted invoice ids for batch payments. Use empty strings for unavailable invoice, vendor, risk, or days-late fields.

## `reconciliation_summary.json`

Use this shape:

```json
{
  "totals": {
    "outgoing_payments": 3,
    "clean_payments": 2,
    "exception_payments": 1,
    "matched_amount": "1200.00",
    "exception_amount": "400.00"
  },
  "by_risk_tier": {
    "low": {"payments": 3, "amount": "20150.40", "exception_payments": 0}
  },
  "exceptions_by_code": {
    "amount_mismatch": 1
  }
}
```

## `adjustment_timeline.json`

Use this shape:

```json
{
  "winning_adjustments": [],
  "superseded": [],
  "skipped_unknown_reference": []
}
```

Include enough identifiers in each adjustment to explain what happened. Sort winning adjustments by `chat_ts`.
