We reconcile customer invoices from a finance workbook and billing corrections posted in a Slack channel that is served to you through the **`billing-slack` MCP server** (there is no Slack export file in the workspace). Gather both sources, apply the corrections, and produce three output files.

The Excel workbook is at `/root/invoice_register.xlsx`. It has multiple monthly sheets in workbook order. Each monthly sheet uses these columns: account_owner, row_type, invoice_id, original_invoice_id, invoice_date, customer_id, customer_name, description, status, currency, amount, paid_amount, due_date, payment_terms_days.

The workbook also has two reference sheets:

- `FX_Rates` with columns `currency` and `usd_rate`. Use this static table to convert `USD`, `EUR`, and `GBP` invoice values into USD.
- `Customer_Aliases` with columns `alias_name`, `canonical_customer_id`, and `canonical_customer_name`. Normalize workbook customer names through this table before writing the ledger or summary.

The `account_owner` cell is sparse: it is filled only at the first row in an owner group, so fill it down for following invoice or credit rows until another owner is shown. Blank rows, separator rows, subtotal rows, and rows without a real `invoice_id` must be skipped.

`row_type` is either `INVOICE` or `CREDIT`. Invoice IDs use `INV-####`; credit memo IDs use `CM-####`. A `CREDIT` row must have `original_invoice_id` pointing to an existing final invoice. Credit memo amounts reduce customer balance: write credit amounts and balances as negative money values in the ledger even if the workbook amount is positive. Credit memos have no paid amount and no reserve.

If an `invoice_id` appears more than once, deduplicate deterministically:

1. Workbook sheet order defines recency.
2. If the same `invoice_id` appears in multiple sheets, keep the row from the latest sheet by workbook order.
3. If the same `invoice_id` appears more than once within the same sheet, keep the later row.
4. Only the final deduplicated invoice or credit memo may appear in the ledger.

The billing corrections are not in the workspace as a file. They were posted in a Slack channel exposed to you through the **`billing-slack` MCP server**. Read the channel **exhaustively** through the MCP: call `slack_list_channels` to find it, then paginate the full history with `slack_read_channel` (follow `next_cursor` until `has_more` is false), and call `slack_read_thread` for every message that has replies. Correction tokens are scattered across message text, attachments, blocks, and threaded replies; `slack_search_messages` is capped and not exhaustive, so do not rely on a single search. Each correction token has this format:

`BILLING_FIX{invoice:INV-####,field:FIELD,to:VALUE,priority:N}`

Allowed correction fields are:

- `status`
- `paid_amount`
- `amount`
- `account_owner`
- `description`
- `add_payment`

`paid_amount` is a direct replacement. `add_payment` is an additive payment event: add the numeric `to` value to the invoice's current `paid_amount` after workbook normalization and other winning corrections for that field. If multiple corrections target the same `invoice_id` and field, apply only the winner: higher priority wins; if priority ties, later Slack `ts` wins. Record losing corrections in `superseded`.

Allowed statuses are `Open`, `Paid`, `Disputed`, and `Void`. Status values are case-insensitive in inputs and corrections, but output status values must use exactly those spellings.

If a correction targets an invoice_id that is not present after workbook deduplication, do not apply it. Record it in `skipped_unknown_invoice`.

Money and FX rules:

- `amount`, `paid_amount`, and `to` values for money corrections must be numeric values.
- Write CSV money fields with exactly two decimal places.
- Write `fx_rate_to_usd` with exactly two decimal places.
- Native ledger money fields stay in the row currency.
- USD ledger fields are `amount_usd`, `paid_amount_usd`, `balance_usd`, and `reserved_amount_usd`, computed with `FX_Rates.usd_rate` and rounded to two decimals.
- Write `customer_summary.json` totals as numeric floats rounded to two decimals. Summary totals must be in USD.
- After all workbook normalization and corrections, recompute `balance = amount - paid_amount` and `balance_usd = balance * fx_rate_to_usd`.
- For `Paid` invoices, final balance must be 0.00. If status is corrected to `Paid`, set `paid_amount = amount`.
- For `Open` invoices, partial payments are allowed and balance may be positive.
- For `Disputed` invoices, partial payments are allowed and balance may be positive.
- For `Void` invoices, set `amount = 0.00`, `paid_amount = 0.00`, and `balance = 0.00`.

Aging and reserve rules:

- Use close date `2026-05-31` for all aging calculations.
- `days_past_due = max(0, close_date - due_date)` in calendar days, but set `days_past_due = 0` when final `balance_usd` is `0.00` or negative.
- `aging_bucket` is `settled` when `balance_usd` is `0.00` or negative, `current` when balance is positive and not past due, `1-30` for 1 to 30 days past due, `31-60` for 31 to 60, and `61+` for more than 60.
- `reserved_amount_usd` estimates allowance exposure from final USD balance. For `Paid`, `Void`, and `CREDIT` rows, reserve is `0.00`. For `Disputed` invoices, reserve is 25% for `current` or `1-30`, 50% for `31-60`, and 75% for `61+`. For `Open` invoices, reserve is 0% for `current`, 2% for `1-30`, 5% for `31-60`, and 20% for `61+`.

Produce `/root/invoice_ledger.csv` with this exact column order:

`row_type,invoice_id,original_invoice_id,invoice_date,due_date,payment_terms_days,customer_id,customer_name,account_owner,description,status,currency,fx_rate_to_usd,amount,paid_amount,balance,amount_usd,paid_amount_usd,balance_usd,days_past_due,aging_bucket,reserved_amount_usd,source_sheet`

Sort the ledger by `invoice_date`, then `invoice_id`. Dates must be in `YYYY-MM-DD` format. Ledger `customer_id` and `customer_name` must be canonical values after alias normalization.

Produce `/root/customer_summary.json` with this shape:

```json
{
  "customers": [
    {
      "customer_id": "CUST-001",
      "customer_name": "Example Co",
      "invoice_count": 3,
      "credit_count": 1,
      "open_count": 1,
      "paid_count": 1,
      "disputed_count": 1,
      "void_count": 0,
      "current_balance_usd": 0.0,
      "days_1_30_balance_usd": 0.0,
      "days_31_60_balance_usd": 0.0,
      "days_61_plus_balance_usd": 500.0,
      "total_amount_usd": 1200.0,
      "total_paid_usd": 700.0,
      "total_balance_usd": 500.0,
      "total_reserved_usd": 175.0
    }
  ],
  "totals": {
    "invoice_count": 10,
    "credit_count": 1,
    "open_count": 4,
    "paid_count": 3,
    "disputed_count": 2,
    "void_count": 1,
    "current_balance_usd": 0.0,
    "days_1_30_balance_usd": 700.0,
    "days_31_60_balance_usd": 1000.0,
    "days_61_plus_balance_usd": 1300.0,
    "total_amount_usd": 10000.0,
    "total_paid_usd": 6500.0,
    "total_balance_usd": 3500.0,
    "total_reserved_usd": 600.0
  }
}
```

Sort customers by `customer_id`. Summary totals, aging balances, credits, FX conversion, and reserve totals must be recomputed from the final ledger after corrections.

Produce `/root/correction_audit.json` with exactly these top-level keys:

- `winning_patches`: applied corrections with slack_ts, invoice_id, field, previous_value, new_value, priority. Sort by slack_ts ascending.
- `superseded`: losing corrections with slack_ts, invoice_id, field, reason.
- `skipped_unknown_invoice`: skipped corrections with slack_ts, invoice_id, field, reason.

In `winning_patches`, `previous_value` and `new_value` must always be JSON strings. For money fields (`amount`, `paid_amount`, `add_payment`, and `balance` if ever present), these strings must use exactly two decimal places, such as `"1100.00"`; do not write `1100.0` or `"1100"`. For `status`, `account_owner`, and `description` corrections, these values are also strings. This string-format rule applies only to audit patch values; `customer_summary.json` totals remain numeric floats rounded to two decimals.

Do not modify input files. All outputs must be valid UTF-8 CSV or JSON.
