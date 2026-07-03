# Per-Token-Type Cost Accounting — Design & Experiment

Status: draft / under review
Owner: charles
Date: 2026-06-30

## 1. Problem

Trial cost in Oddish is computed from token counts pulled out of Harbor's
trajectory / `AgentContext`. The current model has three accuracy gaps. The
first is the only large one.

| Gap | Today | Truth | Magnitude |
|-----|-------|-------|-----------|
| **Cache-write unmodeled** | only `cache_tokens` (read) exists; write priced as input/read | Anthropic bills cache *creation* at **1.25× input** | **Large** on cache-heavy Anthropic runs |
| **Reasoning not separated** | folded into output | Anthropic bills thinking as output; OpenAI `reasoning_tokens` ⊂ completion | **~0 cost**, visibility only |
| **Aggregate single-model pricing** | one model price applied to trial totals | a trajectory may mix models | nonzero only if models vary within a trial |

> NOTE: the original framing ("Harbor gives one lumped total, nothing is
> separated") is inaccurate. `estimate_cost_usd` already splits
> fresh/cache-read/output and prices cache-read at the read rate. The genuine
> work is **cache-write + reasoning + per-call/multi-model sourcing**.

## 2. Current implementation (as-is)

- Cost: `oddish/src/oddish/model_pricing.py::estimate_cost_usd` (L253) —
  `uncached_input·input + cached·cache_read + output·output`.
- Pricing: litellm.model_cost, gap-filled by local `PRICING_TABLE` (L58). No
  `cache_write` rate anywhere.
- Token source: `workers/harbor/outcome.py` — prefers
  `AgentContext.{n_input_tokens,n_cache_tokens,n_output_tokens,cost_usd}`,
  falls back to ATIF `final_metrics.{total_prompt_tokens,total_completion_tokens,
  total_cached_tokens,total_cost_usd}`.
- Storage: `TrialModel.{input_tokens,cache_tokens,output_tokens,cost_usd,
  total_steps,has_trajectory}` (db/models.py L801).
- Native passthrough: if `AgentContext.cost_usd` is set (cursor, codex,
  antigravity self-compute it), Oddish stores it **verbatim and never
  estimates**. So today's accuracy is heterogeneous *by agent*.

### Where the per-token data actually lives

ATIF per-step `Metrics` (harbor/models/trajectories/metrics.py) has **only**
`prompt_tokens / completion_tokens / cached_tokens / cost_usd` and forbids extra
fields *on the model* — cache-write and reasoning can live **only** in
`metrics.extra` / `final_metrics.extra`. Each adapter names them differently:

| Adapter | cache-write key | reasoning key | level |
|---------|-----------------|---------------|-------|
| rovodev_cli, opencode, mimo | `cache_write_tokens` | `reasoning_tokens` | final/extra |
| kimi_cli | `input_cache_creation` | — | per-call extra |
| cursor_cli | `cacheWriteTokens` (+ self-cost) | — | self-cost |
| codex | — | `reasoning_output_tokens` | final/extra |
| mini_swe_agent | — | `total_reasoning_tokens` | final/extra |
| trae_agent, computer_1, base | **absent** | — | — |

**Implication:** there is no uniform "sum every call by type" path. A
normalizer keyed by agent is required, and some agents simply don't expose
cache-write.

## 3. Proposed change (to-be)

1. Schema: add `cache_write_tokens` (+ optional `reasoning_tokens`) to
   `TrialModel`, `HarborOutcome`, the trial response builders, **and the
   matching `load_only` set in `list_tasks_core`** (CLAUDE.md gotcha — else the
   compact experiment page 500s with MissingGreenlet).
2. Normalizer in `outcome.py`: read the per-adapter `extra` keys into a
   canonical `TokenBreakdown{fresh_input, cache_read, cache_write, output,
   reasoning}`. Prefer per-step sums; fall back to `final_metrics.extra`.
3. Pricing: add `cache_write` to `ModelPricing` + `PRICING_TABLE`; extend
   `estimate_cost_usd` to charge `cache_write·cache_write_rate`
   (default 1.25× input when unknown).
4. Keep native `cost_usd`, but persist the estimate alongside so they can be
   compared.

> Cost is linear in tokens ⇒ accurate *cost* needs only correct **totals** with
> the full 5-way split. True per-call summation is required **only** when a
> single trajectory mixes models. Decide that before building per-call infra.

## 4. Experiment — does the new method match reality?

### 4.1 The ground-truth problem

Neither Bedrock nor Azure OpenAI returns a **dollar** cost — only token counts.
So "true cost" = provider-reported token split × official price sheet. The
experiment therefore validates **token attribution**, not arithmetic. Ground
truth must come from the **raw provider `usage` blocks**:

- Bedrock (Anthropic): `usage.cacheWriteInputTokens`, `usage.cacheReadInputTokens`,
  `usage.inputTokens`, `usage.outputTokens`.
- Azure (OpenAI): `prompt_tokens`, `prompt_tokens_details.cached_tokens`,
  `completion_tokens`, `completion_tokens_details.reasoning_tokens`.

Capture these with a **logging proxy** (LiteLLM proxy or mitmproxy) between the
agent and the provider. The proxy log is the gold standard.

### 4.2 Design

- Pick workloads that actually exercise the gaps:
  - **Cache-heavy**: large reused system prompt with `cache_control` set, so
    the first call writes a big cache and later calls read it. *Without
    `cache_control`, Anthropic does no caching and before==after — nothing to
    measure.*
  - **Reasoning-heavy**: extended thinking / high reasoning effort on.
- Run N trials per arm through Oddish with real keys, proxy in front.
- For each trial compute four numbers:
  - **T (truth)** = proxy usage × official prices.
  - **A (native)** = `AgentContext.cost_usd` if present.
  - **B (before)** = current `estimate_cost_usd`.
  - **C (after)** = new per-type `estimate_cost_usd`.
- **Force the estimate path** (ignore native `cost_usd`) for B/C, or the new
  code never runs.
- Metric: per-trial absolute % error vs T; report MAPE and a scatter, before vs
  after, split by provider.

### 4.3 Expected signal

- **Anthropic/Bedrock, cache-heavy** → large before-error (missing 1.25× write
  premium), large after-improvement. This is the experiment's discriminating
  power.
- **OpenAI/Azure** → small before/after gap (reasoning already billed as
  output, no cache-write concept). Mostly a control arm.

## 5. Open decisions (defaults in **bold**)

1. Ground truth = **logging proxy capturing raw `usage`**.
2. Sourcing = **per-adapter normalizer scoped to the two experiment providers**,
   not an upstream ATIF schema change (revisit if it generalizes).
3. Experiment exercises **both** providers, but signal is expected only from
   Anthropic.
4. **Force-estimate** in the experiment arm so the new path is actually tested.
5. Per-call summation: **defer** unless trials are confirmed to mix models;
   ship aggregate-with-full-split first.
6. Cache-write rate when unknown: **1.25× input** (Anthropic default).
