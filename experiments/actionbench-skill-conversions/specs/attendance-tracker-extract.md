# Conversion spec — attendance-tracker-extract → Slack-MCP

**Recipe:** MCP-swap-datasource (Slack-MCP). **Effort:** low — reuses the built
`mcp/mock_slack_mcp.py` verbatim; this is the cheapest remaining conversion.

## Source
`harbor-forge-v2/tasks/skills/attendance-tracker-extract` — extracts an
attendance/RTO incident timeline from an Excel tracker plus a Slack-shaped export
(`rto_whisper_export.json`) carrying `CORRECTION{…}`-style tokens, with a member
map.

## Change
1. Keep the Excel tracker + member map as files.
2. Move the Slack export behind the `attendance-slack` MCP:
   - Seed = the existing `rto_whisper_export.json` reshaped to the channel seed
     shape `{channel, messages:[{ts,user,text,attachments?,blocks?,thread_replies?}]}`
     (it is already close), + token-free noise to force pagination, preserving
     **every** original token and its ts exactly (verify byte-identical, as the
     built tasks do).
   - `task.toml`: add the stdio `[[environment.mcp_servers]]` block pointing
     `args` at the sealed seed under `/opt/slack-mcp/seed/…`.
   - Dockerfile: drop the raw export from the workspace; COPY the MCP +
     seed + a `workspace_readme.md`.
   - instruction.md: repoint the "read the export" step to the MCP (paginate +
     threads + attachments/blocks).
3. Reuse `tests/` verbatim. Repoint `solution/solve.sh` at the seed path.

## Validate (same gates as the built tasks)
- `mock_slack_mcp.py` over the seed recovers 100% of the original tokens via
  pagination + threads (add a case to `mcp/smoke_test.py`).
- oracle → reused verifier passes (add a case to `mcp/validate_oracle.py`).

## Non-triviality
Distribute some tokens into thread replies (search excludes threads) and
attachments so a single search cannot recover them.
