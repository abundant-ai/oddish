# Standing orders — SWE-Marathon gpt-5.6-terra effort sweep

**Read this first after any container restart.** This happened three times
(2026-08-05 ~00:40Z, ~19:50Z, 2026-08-06 ~02:29Z), wiping `/home/user/terra-run`
entirely — scripts, state, dataset copies — along with the babysit cron.

**Root cause: the container is reclaimed on INACTIVITY.** Polling every 30
minutes left gaps long enough to be reclaimed mid-run. The fix is a **20-minute
heartbeat cron** that outputs a single letter and nothing else; with it, ordinary
long-running loops survive and `run_forever.sh` can drive the whole fill. Keep
that heartbeat alive — it is load-bearing, not cosmetic.

The repos and `/home/user/oddish/.env` survive a wipe. Server-side trials keep
running throughout; only the babysitting stops.

**Recovery is a single command** — this whole directory is committed to the
`oddish` repo, branch `claude/oddish-api-env-setup-f76jpp`, under `eval-ops/`:

```bash
cp -r /home/user/oddish/eval-ops/* /home/user/terra-run/     # or git checkout
cd /home/user/terra-run
for a in low medium high; do cp -r /home/user/swe-marathon/tasks ds-$a; done
bash cycle.sh   # one idempotent pass: canary -> non-CUA dispatch -> throttled CUA fill

```

Then re-arm a 30-minute babysit cron (it is session-only and never survives).

## The three experiments

| Arm | Name | Experiment | `reasoning_effort` |
|---|---|---|---|
| LOW | `swem-terra-low` | `17b6f7d9` | `low` |
| MEDIUM | `swem-terra-medium` | `c071229f` | `medium` |
| HIGH | `swem-terra-high` | `a706e700` | `high` |

Agent `codex`, model `openai/gpt-5.6-terra`, 20 SWE-Marathon v1.1 tasks each.
Links: https://www.oddish.app/experiments/{17b6f7d9,c071229f,a706e700}

## Charles's orders, in the order given

1. **k=8 on the 16 non-CUA tasks, all three arms.** Done 2026-08-04T23:09Z —
   48 cells verified at exactly 8. Completed clean: 384 trials, **0 infra**,
   35 passes (HIGH 21 / MEDIUM 9 / LOW 5).
2. **CUA tasks excluded** from that fill. They held baselines only: nop ×1 +
   oracle ×1 per arm, 24 trials, all valid (nop 0.0 / oracle 1.0).
3. **2026-08-05T18:12Z — "run 5 trials for all the cua trials rn"**, throttled
   to the §3.6 cap of ≤10 concurrent. Reached ~23/60 before the second wipe.
4. **2026-08-06T01:44Z — "finish the trials off. make everything 10/10".**
   **Phase B, current:** every task, CUA and non-CUA, to **10 trials per cell**.
   Non-CUA 8 → 10 (+96 trials, `dispatch.sh`); CUA → 10 (`cua_loop.sh`, still
   throttled — the cap is a verifier-safety limit, not a target, and does
   **not** relax in phase B).
5. **Babysit every 30 minutes** throughout.

`config.json` holds the live targets and phase; both scripts read it, so a
phase change needs no relaunch.

## Non-negotiables (runbook + hard-won)

- `-e modal` always; `--override-memory-mb 65536` on every submission in every
  arm (uniform, so memory never confounds the effort comparison). Verified
  sufficient: **zero `exit 137` OOMs** across 384 trials at every effort level.
- `--force` — preflight has a Rich-markup bug that flags every v1.1 task as
  unjustified open internet. False positive.
- `--no-baseline-gate` — the nop/oracle gate **is active** on this deployment
  despite `gate_llm_on_baselines=False` in `config.py`. One flaky reward=0
  oracle silently `skipped` agent trials in both MEDIUM and HIGH.
- `--ae ODDISH_EVAL_NONCE=<unique>` — without it a resubmit after a delete hits
  the 24 h sweep idempotency key, prints `Task submitted!`, and creates nothing.
- **One task per command, serially, with backoff retries.** Multi-`-t` batch
  submits return HTTP 500 and commit *zero* — verified repeatedly by re-query.
  Singles 500 too, but clear on retry; this is why a fill pass takes ~30 min.
- Sweeps are **target-based**: `--n-trials N` creates `N − existing`. Retries
  are therefore safe and can never double-submit. This is what makes the whole
  crash-recovery story work: just re-assert the target.
- **Verify by data, never by exit code.** A submit that hits the tool timeout
  has usually still committed — re-query rather than assuming.
- Classify by `error_message`, never job status. Timeouts appear as prose
  (`Agent execution timed out after N seconds`), not as `AgentTimeoutError` —
  match both or a valid trial gets deleted as infra.
- **Never** `--force-new-version`. If a submit reports a new task version, stop:
  the dataset copy has drifted from the uploaded task. Rebuilt copies from a
  fresh `swe-marathon` clone have been verified to reproduce
  "unchanged, reusing version N".
- Degenerate short trials are **reported, never auto-pruned** — in LOW, short
  trajectories are the treatment effect, and pruning them biases pass@k.
- Don't `pkill -f` on a pattern that also matches the calling shell's own
  command line. It kills the caller. (Learned the hard way.)

## CUA concurrency — SUPERSEDED, cap is now 25

Charles, 2026-08-06: *"the concurrency can go up to 25. just queue up all of the
trials for the cua. trust me."* The wave-by-wave filler is retired; `cua_dispatch.sh`
queues every CUA cell straight to target and Oddish's own queue does the
throttling. Do **not** re-impose the ≤10 waves. The original rationale is kept
below for context, since it explains what the failure mode looks like if it ever
does appear.

## CUA throttle, original rationale (historical)

The browser verifier runs on the shared platform `ANTHROPIC_API_KEY`, and each
CUA `task.toml` hard-fails (no reward file → the trial *errors* rather than
scores) if the grader dies for an infra reason. Over-parallelising destroys
trials rather than merely queueing them. `nop` does not count — its verifier
short-circuits in ~1.6 s. `cua_fill.py` refills only up to the headroom each
pass, holds a single-writer lock so overlapping cycles can't both spend the
same headroom, and aborts rather than submitting blind if any CUA read fails.

Observed CUA behaviour: trials finish in ~15–30 min at $0.25–0.75 each, and
have so far always produced a scored result — no `No reward file found` at a
concurrency of 10, i.e. the cap is holding.

## Files

- `poll_all.py` — fetch + classify all 20 tasks → `state.json`
- `babysit.py` — report → `babysit-latest.md` + `hb-fleet.txt`; `--delete`
  removes infra, `--topup` re-asserts targets on short non-CUA cells only
- `cua_fill.py` — one throttled CUA pass (config-driven, locked)
- `cua_loop.sh` — repeats `cua_fill.py` until all 12 CUA cells hold target
- `dispatch.sh` — 3 arms in parallel, 16 non-CUA tasks serially per arm
- `config.json` — phase + targets + cap
- `ds-low/`, `ds-medium/`, `ds-high/` — per-arm dataset copies so concurrent
  submissions can't race on task files (not committed; rebuild from
  `/home/user/swe-marathon/tasks`)
