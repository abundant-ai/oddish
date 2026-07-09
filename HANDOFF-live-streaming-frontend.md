# Handoff — S4/S5 live-streaming frontend (from a parallel Claude session, 2026-07-08)

For the worker implementing the live transcript surfaces (S4 CLI + S5 dashboard) on
`feat/live-streaming-mvp`. I researched the same task, stood down before mutating
anything; these are my conclusions. Your in-flight files when I stopped:
`frontend/src/components/live-transcript-panel.tsx` (new),
`frontend/src/components/trial-detail-panel.tsx` (modified),
`oddish/src/oddish/cli/logs.py` + `oddish/tests/test_cli_logs.py` (new),
`oddish/src/oddish/cli/__init__.py` (modified). Nothing in this file describes my
code — I wrote none. Do NOT commit this file (repo policy: ad-hoc docs stay uncommitted).

## 1. Backend contract (verified against this branch's code, not the spec)

`GET /trials/{trial_id}/live?attempt=<int?>&after_seq=<int=0>` —
`backend/api/routers/trials.py:226` and `oddish/server/__init__.py`, both via
`oddish/src/oddish/core/trial_live.py`:

```
{ attempt, events: [{seq, kind, payload, created_at}], next_seq,
  usage: {input_tokens, cache_tokens, cache_write_tokens, output_tokens, cost_usd},  # nullable
  harbor_stage, done }
```

- `next_seq` = last event's seq, or the *effective* after_seq when no events.
- If client `attempt` ≠ trial's current attempt, server ignores `after_seq` and returns
  the current attempt's events from seq 0; response `attempt` is current. Client must
  REPLACE (not append) its transcript when response attempt differs from its own.
- Page limit 500 (`LIVE_EVENTS_PAGE_LIMIT`). Full page ⇒ drain: refetch immediately.
- `done` = `finished_at IS NOT NULL` — NOT final (queue auto-retry clears it, see
  d072fc5e). `done` can ride alongside a full page ⇒ on done, drain until an empty page.
- Terminal trials get their events PURGED (worker `finally` + 24h TTL sweep, ac80609f /
  295fadd4) ⇒ `done: true` + zero events is the NORMAL finished state, not an error.

Event payload shapes (from `oddish/src/oddish/workers/harbor/live_tail.py`
`_render_assistant_blocks` / `_render_tool_results` / `_clipped_payload`):

| kind | payload |
|---|---|
| `message` | `{text}` |
| `tool_use` | `{name, input}` — `input` is a JSON-*stringified* clip, render as code not object |
| `tool_result` | `{content, is_error?: true}` |
| `summary` | `{text}` — final result line AND the transcript-capped marker; always render |

Every payload value is clipped server-side; `truncated: true` set when clipped — surface
it. Tolerate unknown kinds (render fallback, never crash).

## 2. Client loop invariants (the part reviews will hammer)

State: `(attempt, afterSeq, events[], usage, stage, done)`. Tick every 2s (spec §8):
append on same attempt / replace on new attempt; `afterSeq = next_seq`; immediate
refetch on full page; stop only at `done && page empty`; on fetch error keep polling
and show a degraded indicator after ~5 consecutive failures; AbortController on unmount.
Watch for: React StrictMode double-mount double-polling, stale closures over the cursor,
setTimeout leak on unmount.

Expected UX quirks that are correct behavior (spec §6, don't "fix"): live cost is a
LiteLLM estimate that visibly SNAPS at completion when the authoritative
`total_cost_usd` overwrites it; empty transcript is the COMMON case (worker flag
`live_tail_enabled` defaults false; non-Claude-Code agents have no adapter) — waiting
state, with harbor_stage still progressing.

## 3. Frontend map (line refs verified on main, 2026-07-08)

- Two-hop rule: browser → Next route handler → backend. New proxy route
  `frontend/src/app/api/trials/[trial_id]/live/route.ts`; clone
  `.../trajectory/route.ts`; forward the query string wholesale — idiom in
  `frontend/src/app/api/chat-sessions/[id]/events/route.ts`
  (`request.nextUrl.searchParams.toString()`).
- Mount: `frontend/src/components/trial-detail-panel.tsx` — `validTabs` ~346,
  TabsList ~874-909, TabsContent ~1140, lazy `next/dynamic({ssr:false})` imports ~71-98
  (TrajectoryViewer is the structural template, props `{trialId, apiBaseUrl="/api"}`).
  Tab syncs to URL `?tab=` (~368-401). The panel is shared with
  `experiment-detail-view.tsx:1363` ⇒ the tab appears in both tasks and experiments.
  "Running" gate precedent: `hasLiveQueueSnapshot` ~202
  (`["queued","retrying","running","pending"].includes(trial.status)`).
- **SWR trap:** global `SWRConfig` in `src/app/providers.tsx` sets
  `dedupingInterval: 5000` — a 2s `refreshInterval` poll gets clamped; and cursor
  accumulation doesn't fit SWR's per-key cache. Hand-roll the loop (closest precedent:
  `src/components/cc-chat/use-chat-session.ts`). If you do use SWR anyway, override
  `dedupingInterval` per-hook and keep the accumulator outside SWR.
- **Drawer snapshot trap:** the panel's `trial` prop is snapshotted at open
  (`task-detail-client.tsx` `handleSelectTrial` ~737; the 30s task poll never re-feeds
  an open drawer) — the live tab must be fully self-sufficient from the /live response
  (it is: usage + stage + done all come back each tick).
- Reuse: `HarborStageBadge` (`harbor-stage-badge.tsx`), `formatCostUsd`
  (`src/lib/format.ts:3`; estimate-marker precedent `costEstimateMarks`), `CodeBlock`
  (`src/components/code-block.tsx`) for tool_use input / tool_result content, tokens via
  `.toLocaleString()` (color precedent trajectory-viewer TokenUsageBar: cached=emerald,
  prompt=blue, completion=purple). Types go in `src/lib/types.ts` (Trajectory block
  ~486-555; note `Trial` has no `cache_write_tokens` field yet).
- Verification: `cd frontend && pnpm lint && pnpm build` — **there is no src typecheck
  script; `pnpm build` IS the typecheck.** Prettier enforced (`pnpm format`). No
  frontend unit-test harness.

## 4. CLI slice (S4) notes

`done`-handling contract applies identically: key on `attempt` (a newer attempt after
`done:true` is a restart, not an error), drain until empty page after done, then fall
back to the post-hoc S3 transcript. Spec §8/§11 in
`docs/superpowers/specs/harbor-live-streaming-mvp.md` (committed on this branch).

## 5. Session cleanup state

- I created NO branch and NO file changes; a `feat/live-streaming-dashboard` branch was
  planned but never created. If one appears later it's stray — safe to delete.
- Owner preference (from CLAUDE.md/memory): terse comment-free code; PRs brought to
  ready but the user runs merges; Bugbot skips agent-authored PRs — comment `bugbot run`
  to trigger.
