# Conversion spec — coral-sponge → SQL/query-MCP

**Recipe:** MCP-swap-datasource (SQL-MCP). **Effort:** medium — needs a new
generic `sqlite-query` stdio MCP (write once, reused by `sql-etl-notebook-join-debug`).

## Source
`harbor-forge-v2/tasks/skills/coral-sponge` — a hard (≈5.5h) spatial-stats
pipeline. It already ships a real **SQLite** DB `observations.db`
(`observations`, `quality_flags`, `species`, `surveys`) and the reference
solution issues `sqlite3` + `pd.read_sql_query` JOINs; plus a bathymetry raster
`CatalinaBathymetry.tif` and a points geojson.

## Change
1. Keep the raster `.tif` and the geojson as files (geospatial inputs).
2. Move `observations.db` behind a `marine-db` MCP. Write a generic
   `mcp/sqlite_query_mcp.py` (stdio JSON-RPC, argv = sealed `.db` path) exposing:
   - `list_tables()`
   - `describe_table(table)` → columns + types + row count
   - `run_query(sql)` → rows (read-only: reject non-SELECT; cap rows per call,
     e.g. 500, and require `LIMIT`/`OFFSET` paging for large reads)
   Seal the `.db` under `/opt/db/` (out of workspace), declare the stdio MCP in
   `task.toml`, drop the raw `.db` from the workspace, repoint instruction.md and
   `solution/solve.sh` (the oracle may open the sealed `.db` directly).
3. Reuse `tests/` verbatim (outputs unchanged).

## Non-triviality (avoid the "one query then offline math" anti-pattern)
The agent must **discover the schema** (`list_tables`/`describe_table`), then run
the multi-table quality-flag JOIN + filter and page results — not a single
`SELECT *`. Enforce a per-call row cap so the full filtered set requires paged
queries. The downstream spatial statistics remain the hard offline core; the
SQL-MCP makes data acquisition non-trivial without being the whole task.

## Validate
- A smoke test that drives the SQL-MCP, discovers the schema, and reproduces the
  filtered observation set the solution expects (paged).
- oracle → reused verifier passes.

## Note
SQL-MCP gathering is a weaker non-triviality signal than Slack pagination; pair
with the per-call row cap + schema discovery to keep it honest. This is why the
two Slack-MCP tasks were built first.
