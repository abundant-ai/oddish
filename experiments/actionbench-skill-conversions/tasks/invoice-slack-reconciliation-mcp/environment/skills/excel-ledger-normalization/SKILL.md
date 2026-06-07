---
name: excel-ledger-normalization
description: Normalize multi-sheet finance invoice workbooks into a clean ledger. Use when invoice rows contain sparse account owners, separator rows, duplicate invoice IDs, dates, statuses, and money fields that must be standardized before reconciliation.
---

# Excel Ledger Normalization

Use this workflow for finance workbooks that contain one invoice register spread across multiple monthly sheets.

## Workbook Pattern

Typical columns:

```text
account_owner, row_type, invoice_id, original_invoice_id, invoice_date, customer_id, customer_name, description, status, currency, amount, paid_amount, due_date, payment_terms_days
```

Common real-world issues:

- account owner cells are sparse and need fill-down
- blank, separator, and subtotal rows are mixed into the ledger
- invoice IDs appear more than once after revisions
- money values may be numbers or strings
- dates may be Excel datetime values or parseable text
- status capitalization may vary
- due dates and payment terms may drive downstream aging calculations
- credit memo rows may need to be retained as negative-balance ledger rows and linked to an original invoice
- reference sheets may define customer aliases or currency conversion rates

## Extraction Procedure

1. Load with `openpyxl.load_workbook(path, data_only=True)`.
2. Iterate sheets in workbook order and keep the sheet index.
3. Find the header row by matching expected column names case-insensitively.
4. Track the current non-empty account owner while scanning rows.
5. Treat a row as an invoice row only when `invoice_id` looks like a real invoice identifier and required business columns are present.
6. Skip rows that are blank, separators, subtotals, notes, or section headings.
7. Normalize dates to `YYYY-MM-DD`.
8. Normalize money to decimal numeric values before doing math.
9. Normalize status values to the allowed canonical spellings for the task.
10. Preserve due dates and payment terms so aging and reserve calculations can be recomputed after all corrections.
11. Apply customer alias mappings before grouping or summarizing.
12. Apply static FX rates only after native-currency amounts are normalized.
13. Treat credit memo rows as first-class ledger records, but validate their original-invoice link.

## Deduplication Pattern

When workbook order defines recency, store one record per invoice ID and replace older records as later rows are encountered. If the same invoice appears twice in one sheet, the later row replaces the earlier row naturally.

Keep provenance such as `source_sheet`; it is useful for auditability and verifier checks.

## Finance Pitfalls

- Do not count subtotal rows as invoices.
- Do not leave account owner blank when a prior owner cell should be filled down.
- Do not compare money as strings.
- Recompute derived fields after corrections, not before.
- Aging buckets and allowance/reserve estimates must be based on final balances.
- Preserve the final deduplicated invoice only; do not emit duplicate invoice IDs.
- Customer summaries should group canonical customers, not raw alias spellings.
- Multi-currency summaries should use the requested reporting currency instead of adding native amounts directly.
