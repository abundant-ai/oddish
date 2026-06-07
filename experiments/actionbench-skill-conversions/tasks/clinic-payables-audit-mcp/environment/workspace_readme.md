# Clinic payables reconciliation

Inputs in this workspace (files):

- `/root/invoice_ledger.csv`
- `/root/bank_statement.csv`
- `/root/vendor_master.csv`
- `/root/bank_change_tickets.csv`

The finance-ops payment corrections are **not** in this workspace. They were
posted in a chat channel exposed to you through the **`finance-ops` MCP server**.
Read the channel through the MCP tools (`slack_list_channels`,
`slack_read_channel`, `slack_read_thread`, `slack_search_messages`). Paginate the
full history and open threads — adjustment tokens are scattered across message
text, attachments, blocks, and threaded replies, and search results are capped.

Produce `/root/reconciled_payments.csv`, `/root/reconciliation_summary.json`, and
`/root/adjustment_timeline.json` as described in the task instructions.
