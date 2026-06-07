Finance operations exports weekly invoice ledgers, bank disbursements, vendor master data, and a chat channel where payment corrections are posted (the chat is served to you through the **`finance-ops` MCP server**, not as a file). Reconcile the inputs, apply the finance-ops adjustments, and produce the final audit outputs. Do the work now and write the required files; do not stop to ask for approval or propose a plan.

Input files:

- `/root/invoice_ledger.csv`
- `/root/bank_statement.csv`
- `/root/vendor_master.csv`
- `/root/bank_change_tickets.csv`
- the finance-ops chat channel — served through the **`finance-ops` MCP server** (not a file)

Requirements:

1. Read the invoice ledger, bank statement, vendor master, and bank-change ticket files, and read the finance-ops chat channel through the `finance-ops` MCP.
2. Treat only negative bank-statement rows as outgoing vendor payments. Ignore deposits and bank fees that are not negative vendor disbursements.
3. Match payments to invoices by invoice tokens such as `INV-5001` in the bank memo. Invoice matching is case insensitive. Some payment memos contain multiple invoice tokens for one batch disbursement; in that case resolve all known invoice tokens, sort them, and write the `invoice_id` field as a semicolon-separated list such as `INV-5010;INV-5011`.
4. Read the finance-ops corrections from the **`finance-ops` MCP server**. Call `slack_list_channels`, paginate the full channel with `slack_read_channel` (follow `next_cursor` until `has_more` is false), and call `slack_read_thread` for every message with replies; `slack_search_messages` is capped, so paginate rather than relying on one search. Search message text, attachments, blocks, and thread replies for adjustment tokens:

   `FINCORR{type:remap,payment:PAY-####,invoice:INV-####,priority:N}`
   `FINCORR{type:hold,invoice:INV-####,reason:VALUE,priority:N}`
   `FINCORR{type:release,invoice:INV-####,reason:VALUE,priority:N}`

   For remap adjustments, group by payment id and choose the highest priority adjustment. If priorities tie, choose the later chat timestamp. For hold/release adjustments, group by invoice id and use the same winner rule. Record lower-priority or older tied adjustments as superseded. Record adjustments that reference an unknown payment or invoice in `skipped_unknown_reference`.
5. Apply winning remaps before classifying payments. A remapped payment should use the remapped invoice even when the memo points elsewhere or has no invoice token.
6. Apply winning holds/releases after matching. A winning hold makes every payment for that invoice use `match_status` `held` and include exception code `held_by_finance_ops`. A winning release does not add a hold exception.
7. Write `/root/reconciled_payments.csv` with one row per outgoing bank payment, sorted by `payment_date` then `payment_id`. Columns must be:

   `payment_id,payment_date,invoice_id,vendor_id,vendor_name,amount,currency,match_status,days_late,risk_tier,exception_code`

8. Classify each outgoing payment. Use this status priority when more than one condition applies: `held`, `duplicate_invoice_payment`, `amount_mismatch`, `unmatched`, `remapped`, `matched`.
   - `matched` when the payment has a valid invoice and no exceptions.
   - `remapped` when a chat remap supplied the invoice and no exceptions remain.
   - `unmatched` when no valid invoice can be resolved.
   - `amount_mismatch` when the payment amount does not equal the invoice amount.
   - `duplicate_invoice_payment` when more than one payment resolves to the same invoice.
   - `held` when a winning hold applies.
   Risk, lateness, and account-suffix exceptions can appear in `exception_code` without changing `match_status` unless a higher-priority status above applies.
9. Populate `exception_code` with pipe-separated codes in this order when they apply:
   `unmatched_payment`, `amount_mismatch`, `duplicate_invoice_payment`, `held_by_finance_ops`, `high_risk_late_payment`, `account_last4_mismatch`.
10. Compute `days_late` as payment date minus invoice due date for resolved invoices. For batch payments with multiple invoices, use the largest days-late value across the resolved invoices. Use an empty string for unresolved payments.
11. Write `/root/reconciliation_summary.json` with:
   - `totals`: `outgoing_payments`, `clean_payments`, `exception_payments`, `matched_amount`, and `exception_amount`. `matched_amount` is the sum of outgoing payments that resolve to a known invoice, including rows that still have exceptions. `exception_amount` is the sum of outgoing payments with at least one exception code.
   - `by_risk_tier`: one object per risk tier with `payments`, `amount`, and `exception_payments`.
   - `exceptions_by_code`: counts for each exception code.
12. Write `/root/adjustment_timeline.json` with:
   - `winning_adjustments`: winning chat adjustments with `chat_ts`, `type`, `payment_id` when applicable, `invoice_id`, `reason` when applicable, and `priority`, sorted by `chat_ts`.
   - `superseded`: losing adjustments with enough fields to identify the losing payment or invoice and reason `lower priority or older timestamp on tie`.
   - `skipped_unknown_reference`: ignored adjustments with `chat_ts`, `type`, the unknown reference, and reason `unknown payment or invoice`.
13. Apply vendor control rules:
   - A high-risk vendor payment is a `high_risk_late_payment` when its `days_late` value is greater than zero.
   - A payment whose account suffix differs from the vendor master approved suffix is an `account_last4_mismatch` unless `/root/bank_change_tickets.csv` contains an `approved` ticket for the same vendor and same account suffix with `effective_date` on or before the payment date.
   - A payment can still be clean when it is short-paid by an early-payment discount. The allowed discount is `invoice_amount * discount_bps / 10000` from the vendor master, rounded to cents, and only applies when the payment is at least five days before the invoice due date. For batch payments, compare the payment amount to the sum of each invoice amount after applying any invoice-level eligible discount.

Constraints:

- Do not modify input files.
- Use UTF-8 for all JSON and CSV outputs.
- Keep numeric amounts as two-decimal strings in the CSV.
- Do not require external services, paid APIs, or private credentials.

Success criteria:

- `/root/reconciled_payments.csv` exists with one row per outgoing payment and the required columns.
- `/root/reconciliation_summary.json` contains correct totals, risk-tier rollups, and exception counts.
- `/root/adjustment_timeline.json` records winning, superseded, and skipped chat adjustments.
