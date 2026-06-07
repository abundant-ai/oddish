# ActionBench skill-conversion experiment

This experiment converts existing **harbor-forge-v2 `skills/`** tasks into
**ActionBench-style** Harbor tasks — where non-trivial **tool use is critical** —
and runs them on Oddish with the Cursor CLI agent.

## Why

The `skills/` tasks are deterministic, file-based data-processing tasks with no
live tools (`mcp_servers = []`). ActionBench measures whether a model "effectively
and creatively discovers and uses the tools at its disposal." The bridge: many
`skills/` tasks already contain hard domain logic **and** a robust deterministic
verifier, and only differ from an ActionBench task in their *data-access layer* —
they read a static file where ActionBench would force a live MCP. Swapping the
static corrections file for a **mock Slack MCP** (the "MCP-swap-datasource"
recipe) converts the task in place: the agent must now discover the channel,
paginate it, open threads, and inspect attachments/blocks to recover the data —
while the domain logic and the verifier are reused unchanged.

This is the same mechanism ActionBench's own `slack-support-metrics` /
`databricks-*` tasks use (a seeded mock MCP), so the converted tasks are
directly comparable.

## What is built (2 of the top-5 low-hanging-fruit candidates)

Both are fully built and **locally validated**:

| Task | Source skills task | Files kept | Moved behind MCP | Verifier |
| --- | --- | --- | --- | --- |
| `invoice-slack-reconciliation-mcp` | `invoice-slack-reconciliation` | `invoice_register.xlsx` | billing corrections → `billing-slack` MCP | reused verbatim (5 pytest checks) |
| `clinic-payables-audit-mcp` | `clinic-payables-audit` | ledger/bank/vendor/tickets CSVs | finance-ops corrections → `finance-ops` MCP | reused verbatim (4 pytest checks) |

### Validation evidence (run locally in this repo)

- **All correction tokens are recoverable through the MCP** by paginating the
  channel and reading every thread: invoice 21/21 `BILLING_FIX` (5 pages, 8
  threads), clinic 6/6 `FINCORR` (3 pages, 5 threads). `slack_search_messages`
  is capped **and does not surface thread replies**, so corrections posted in
  threads are reachable only via `slack_read_channel` pagination +
  `slack_read_thread` — a single search cannot recover them. This avoids the
  spec's "trivial search then offline compute" anti-pattern.
- **Oracle → reused verifier passes** end to end: the oracle reads the channel
  seed directly and produces outputs that pass all reused pytest checks
  (invoice 5/5, clinic 4/4). Because the seed preserves every original token
  (verified byte-identical) and adds only token-free noise, the expected outputs
  are unchanged.

To reproduce the local validation:

```bash
cd experiments/actionbench-skill-conversions
# MCP recovers all tokens (drives the stdio server over JSON-RPC):
python3 mcp/smoke_test.py
# Oracle -> reused verifier (needs openpyxl/pandas/pytest):
python3 mcp/validate_oracle.py
```

## How to run on Oddish

```bash
cd experiments/actionbench-skill-conversions
oddish run -p tasks -c sweep.yaml -E actionbench-skill-conversions
```

`sweep.yaml` runs `cursor-cli` (the measurement, 5 trials) plus two sanity
agents: `oracle` (expected reward 1.0 — wiring check) and `nop` (expected reward
0.0 — floor check). Run a single task with
`oddish run -p tasks -t invoice-slack-reconciliation-mcp -c sweep.yaml`.

## How the MCP wiring works

Each task's `task.toml` declares a stdio MCP server, which Harbor's `cursor-cli`
agent writes into `~/.cursor/mcp.json`:

```toml
[[environment.mcp_servers]]
name = "billing-slack"
transport = "stdio"
command = "/usr/local/bin/mock-slack-mcp"
args = ["/opt/slack-mcp/seed/billing_corrections_channel.json", "billing-slack"]
```

`mcp/mock_slack_mcp.py` is a single self-contained stdio JSON-RPC server reused
by both tasks (the channel data is the argv seed). Tools: `slack_list_channels`,
`slack_read_channel` (paginated, attachments/blocks inline, reply_count),
`slack_read_thread`, `slack_search_messages` (capped).

## Sealing / hardening note

The harbor `skills/` environment runs the agent as **root**, so file permissions
cannot fully seal the seed. This conversion ships a **soft seal**: the seed lives
under `/opt/slack-mcp/seed` (outside the workspace, never named in the prompt),
and the prompt + workspace README direct the agent to the MCP. The spec
deprioritizes adversarial sealing ("adversarial testing not critical … primarily
important that sufficient rollouts are observed without the model cheating").

Production hardening — to fully prevent a raw-seed read — mirrors ActionBench's
`slack-support-metrics`: add a non-root agent user, move the seed to a
root-owned `0700` path, and serve it from a small **root-owned seed daemon** over
a localhost socket that the stdio MCP forwards to (so the agent process never has
seed access). The current single-process server is structured to make that split
straightforward.

## Status / caveats

- Locally validated: MCP token recovery + oracle→verifier (above). **Not** yet
  run through `docker build` or the Oddish cloud here — run the sweep to execute
  end-to-end; the `oracle`/`nop` sanity agents confirm wiring on first run.
- Grading stays **deterministic** (reused pytest). Acceptable per spec, though
  below the "≥60% LLM-judge" prior; an LLM-judge rubric could be layered on later.

## Remaining candidates (specs only)

The other three of the top-5 low-hanging-fruit conversions are specified under
`specs/` (ready to build with the same recipe):

- `specs/attendance-tracker-extract.md` — Slack-MCP (same template as the two built)
- `specs/coral-sponge.md` — SQL/query MCP over the shipped SQLite DB
- `specs/sql-etl-notebook-join-debug.md` — SQL/query MCP over the shipped SQLite warehouse
