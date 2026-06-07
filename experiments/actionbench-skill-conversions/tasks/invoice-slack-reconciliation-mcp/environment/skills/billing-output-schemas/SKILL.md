---
name: billing-output-schemas
description: Produce finance reconciliation outputs for invoice ledger CSVs, customer summary JSON, and correction audit JSON. Use when final artifacts must have stable schemas, money precision, status counts, and cross-file consistency.
---

# Billing Output Schemas

Use this checklist before writing finance reconciliation outputs.

## Ledger CSV

Use a stable column order. A typical invoice ledger includes:

```text
row_type, invoice_id, original_invoice_id, invoice_date, due_date, payment_terms_days, customer_id, customer_name, account_owner, description, status, currency, fx_rate_to_usd, amount, paid_amount, balance, amount_usd, paid_amount_usd, balance_usd, days_past_due, aging_bucket, reserved_amount_usd, source_sheet
```

Recommended invariants:

- one row per final invoice ID
- dates in `YYYY-MM-DD`
- money fields formatted with two decimal places
- FX rates formatted consistently and applied before summary totals
- status values canonicalized
- customer aliases normalized before grouping
- sorted deterministically, usually by date and invoice ID
- balance recomputed from final amount and paid amount
- aging fields and reserve amounts recomputed from final balances, not raw workbook rows
- credit memos represented consistently as credit rows with an original-invoice link

## Customer Summary JSON

Group by customer ID. Sort customer objects by customer ID.

Include:

- invoice count
- status counts
- total amount
- total paid
- total balance
- aging bucket balances
- total reserve or allowance exposure when required
- credit memo counts when credits are in the ledger
- reporting-currency totals when the ledger contains multiple currencies

Recompute all totals from the final ledger, not from pre-correction workbook rows.

## Correction Audit JSON

Use three top-level lists:

```json
{
  "winning_patches": [],
  "superseded": [],
  "skipped_unknown_invoice": []
}
```

Winning patches should include target ID, field, previous value, new value, priority, and Slack timestamp. Sort them by Slack timestamp for reviewability.

Store audit patch values as strings. In winning patches, `previous_value` and `new_value` should always be JSON strings. For money fields such as `amount`, `paid_amount`, `add_payment`, and `balance`, format those strings with exactly two decimal places, for example `"1100.00"`, not `1100.0` and not `"1100"`. Status, owner, and description patch values should also be strings. Keep this rule scoped to audit patch values; summary JSON totals should remain numeric floats rounded to two decimals.

## Final Consistency Checks

- Ledger totals match customer summary totals.
- Status counts in summary match ledger rows.
- Audit entries do not refer to applied changes that are absent from the ledger.
- Unknown targets are skipped rather than creating new ledger records.
