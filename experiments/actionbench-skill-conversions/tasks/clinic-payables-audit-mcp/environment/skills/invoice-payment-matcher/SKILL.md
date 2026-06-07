---
name: invoice-payment-matcher
description: Use this skill when matching vendor payments to invoice ledgers and classifying unmatched, amount-mismatch, duplicate, and remapped payments.
---

# Invoice Payment Matcher

## When To Use

Use this skill when a task asks for payment-to-invoice reconciliation from a ledger and a bank export.

## Workflow

1. Build an invoice lookup keyed by normalized invoice id.
2. Resolve each payment to one or more invoices from memo tokens or from an approved correction/remap source.
3. Apply remaps before final classification.
4. For batch payments, sort invoice ids and join them with `;` in the output.
5. Compare payment amount against the expected invoice amount after converting values to decimals. For batch payments, use the sum of expected invoice amounts.
6. Count how many outgoing payments resolve to each invoice; more than one payment for the same invoice is a duplicate payment exception.
7. Calculate date-based metrics only after a payment resolves to a known invoice; for batch payments use the largest days-late value.

## Classification Guidance

- `matched`: valid invoice, correct amount, no exception.
- `remapped`: invoice came from a correction/remap and no exception remains.
- `unmatched`: no valid invoice can be resolved.
- `amount_mismatch`: payment amount differs from invoice amount.
- `duplicate_invoice_payment`: more than one payment resolves to the same invoice.

When several exceptions apply, keep all exception codes in a stable order, but choose the main status according to the task's stated priority.
