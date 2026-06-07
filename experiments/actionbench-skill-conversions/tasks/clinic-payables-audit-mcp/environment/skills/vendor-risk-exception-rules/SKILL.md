---
name: vendor-risk-exception-rules
description: Use this skill when reconciliation outputs must combine vendor master data, risk tiers, due dates, approved bank-account metadata, and exception summaries.
---

# Vendor Risk Exception Rules

## When To Use

Use this skill when finance operations tasks require vendor-risk enrichment or exception rollups.

## Workflow

1. Load vendor master records keyed by vendor id.
2. Enrich matched payments with vendor name, risk tier, and approved account metadata.
3. Calculate `days_late` as payment date minus invoice due date.
4. Flag late high-risk payments when the vendor risk tier is `high` and `days_late` is greater than zero.
5. Flag account mismatches when the bank account suffix does not equal the vendor's approved suffix, unless an approved bank-change ticket for that vendor and account suffix is effective on or before the payment date.
6. Apply early-payment discount controls only when the vendor has a discount rate and the payment is at least five days before the invoice due date.
7. Summarize payments by risk tier after all matching and exception logic has run.

## Common Mistakes

- Using invoice date instead of due date for lateness.
- Treating medium-risk vendors as high-risk vendors.
- Checking account suffixes before the invoice and vendor are resolved.
- Treating rejected or pending account-change tickets as approved.
- Flagging amount mismatch for a valid early-payment discount.
- Forgetting to include exception payments in both totals and risk-tier rollups.
