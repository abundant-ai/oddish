---
name: bank-statement-normalizer
description: Use this skill when reconciling bank statement exports where outgoing payments are represented as negative amounts and invoice references are embedded in memo text.
---

# Bank Statement Normalizer

## When To Use

Use this skill for finance reconciliation tasks that require turning bank statement rows into normalized outgoing payment records.

## Workflow

1. Read statement rows with a structured CSV parser.
2. Treat only negative amounts as outgoing payments unless the task explicitly says otherwise.
3. Convert outgoing payment amounts to positive values for reporting and matching.
4. Preserve the original payment identifier, posted date, currency, memo, and account metadata.
5. Extract invoice references from memo text case-insensitively with a pattern like `INV-\d+`.
6. Preserve all invoice tokens when a memo contains a batch payment such as `INV-5010 / INV-5011`; do not collapse to the first token.
7. Keep unmatched payments in the output. Do not silently drop disbursements just because they lack invoice tokens.

## Common Mistakes

- Including positive deposits in the reconciled payment list.
- Comparing negative bank amounts directly to positive invoice amounts.
- Missing lowercase invoice tokens such as `inv-5002`.
- Missing second or third invoice tokens in batch-payment memos.
- Dropping rows that do not contain an invoice token before checking for correction data.
