# Conversion spec — sql-etl-notebook-join-debug → SQL/query-MCP

**Recipe:** MCP-swap-datasource (SQL-MCP). **Effort:** low *after* the generic
`sqlite_query_mcp.py` from the coral-sponge spec exists — then this is a reseed +
config change.

## Source
`harbor-forge-v2/tasks/skills/sql-etl-notebook-join-debug-colab` — a hard (≈5h)
notebook task that reconciles a closed-period marketing view from a shipped
**SQLite** warehouse `marketing.db` (`channels`, `daily_spend`, `conversions`).

## Change
1. Move `marketing.db` behind a `warehouse` MCP using the generic
   `mcp/sqlite_query_mcp.py` (`list_tables`, `describe_table`, `run_query` with a
   per-call row cap forcing paged reads).
2. Seal the `.db` under `/opt/db/`, declare the stdio MCP in `task.toml`, drop the
   raw `.db` from the workspace, repoint instruction.md and `solution/solve.sh`.
3. Reuse `tests/` verbatim.

## Non-triviality
The agent must discover the 3-table schema and compose the daily-orders
aggregation/join across paged queries to reconstruct the closed-period view —
no single dump. The join-debugging logic remains the hard offline core.

## Validate
- Smoke test: drive the SQL-MCP, discover schema, reproduce the reconciled view
  the solution expects (paged).
- oracle → reused verifier passes.
