# ABT-725 — Speed up preview DB generation

## Problem

Measured on a real `pr-preview` run: the `prepare-preview-database` job is
~9 min, and **451s (≈95%) is `seed()` writing 377,621 sampled prod rows** into
a Micro-sized Supabase branch. Branch provisioning (6s), migrations (5s), and
sampling (30s) are negligible. The seed re-runs on *every* push.

The large caps came from #278, which raised the draw ~50x to ~311k rows once
the COPY + delta-merge loader made big loads cheap (then CI-measured at 173s).
That load cost has since drifted to 451s.

## Changes

### 1. Revert sample volume to the pre-#278 (small) caps — `backend/preview_seed.py`

```
SAMPLE_RECENT_EXPERIMENTS    2000 -> 8
SAMPLE_RANDOM_EXPERIMENTS    4000 -> 8
SAMPLE_EXPERIMENTS_PER_OWNER    5 -> 3
SAMPLE_EXTRA_TASKS           3000 -> 20
SAMPLE_TRIALS_PER_EXPERIMENT  100 -> 50
SAMPLE_SKILLS                 200 -> 10
SAMPLE_DOCUMENTS              200 -> 10
SAMPLE_PROBE_PRESETS          100 -> 10
```

Per-owner coverage (the dashboard "Mine" view guarantee) is preserved by the
per-owner anchor independent of the recent/random caps.

Plus one minimal test pinning the per-experiment trials cap.

### 2. Gate the seed on code-only pushes — `prepare_preview_database.sh`

Run the seed only when migrations ran or the branch was just created (same
condition as the migration step). A code-only re-push reuses the existing
branch data instead of re-COPYing the full deterministic draw.

## Deferred (non-trivial)

Running the seed in a job parallel to the backend deploy — the backend reads
the branch DB URL from a Modal secret, so a separate seed job would need to
carry the ephemeral DB password across jobs and is only verifiable via a live
preview run.
