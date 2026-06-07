# Month-end billing reconciliation

Inputs in this workspace:

- `/root/invoice_register.xlsx` — the multi-sheet invoice workbook (a file).

The billing corrections are **not** in this workspace. They were posted in a
Slack channel that is exposed to you through the **`billing-slack` MCP server**.
Discover the channel and read it through the MCP tools (`slack_list_channels`,
`slack_read_channel`, `slack_read_thread`, `slack_search_messages`). You must
paginate the full channel history and open threads — corrections are scattered
across message text, attachments, blocks, and threaded replies, and search
results are capped.

Produce `/root/invoice_ledger.csv`, `/root/customer_summary.json`, and
`/root/correction_audit.json` as described in the task instructions.
