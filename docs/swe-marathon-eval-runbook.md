# Runbook: full vendor-model eval on SWE-Marathon (via Oddish, on Modal)

This is the operational playbook for running a full SWE-Marathon eval (e.g. a
vendor asks for **5 trials × 20 tasks = 100 trials** on a new/internal model)
end to end on Oddish + Modal. It captures the gotchas learned running xAI
(`grok-build`) and Meta (`mini-swe-agent`) evals so the next one is fast.

**Target outcome:** ≥5 *valid* trials per task, where **valid = the trial ran to
a real terminal state** — a clean success *or* an `AgentTimeoutError`. Any other
infra failure (non-zero agent exit like 137/143/1, verifier infra failure, a
Harbor `ExceptionGroup`) is **not** valid; delete and rerun those.

## 0. Prereqs / setup

1. `git pull` both `oddish` and `swe-marathon`. Treat
   `swe-marathon/tasks/dataset.toml` as the canonical 20-task manifest (including
   registry digests), and each `swe-marathon/tasks/<name>/task.toml` as the
   source of truth for phase network policy, resources, and timeouts. Do not
   reuse a task list or resource table from an older eval.
2. Confirm the agent+model are **routed** in `oddish/src/oddish/config.py` and
   `oddish/src/oddish/workers/harbor/agent_config.py`. A new vendor needs: a
   provider prefix + queue key (`is_*_model` / `to_*_model_id`), env injection
   (`get_*_agent_env`), and network-allowlist coverage. Examples already wired:
   `xai/` → `grok-build`; `meta/` → `mini-swe-agent` (LiteLLM `openai/` route).
3. The vendor API key must be a **Modal secret with the exact env var name the
   route reads** (e.g. `XAI_API_KEY`, `META_API_KEY`) and must be present in the
   **worker** function's env (not just the API), then **deployed**. Symptom of a
   missing/empty key: the agent fails fast (2 steps, 0 tokens) with the
   provider SDK's "missing credentials" error. Note `resolve_env_vars` *raises*
   `Environment variable 'X' not found` only when the var is entirely absent, so
   a "missing credentials" error instead means the var exists but is empty or
   isn't wired where the client reads it (e.g. LiteLLM's `openai/` provider reads
   `OPENAI_API_KEY`, not a vendor-specific name).
4. CLI points at hosted Oddish: `ODDISH_API_URL` + `ODDISH_API_KEY`.

## 1. Confirm the current 20 tasks and execution classes

The current SWE-Marathon v1.1 split, derived from `tasks/dataset.toml`, each
task's `task.toml`, and `scripts/run-benchmark.sh`, is:

- **Open internet (5):** `excel-clone`, `mastodon-clone`, `s3-clone`,
  `slack-clone`, `stripe-clone`.
  - Four use a CUA verifier: `excel-clone`, `mastodon-clone`, `s3-clone`, and
    `slack-clone` (browser-agent verifier on an Anthropic key).
  - `stripe-clone` is open but does not use the CUA verifier.
- **Internet restricted (15):** `biofabric-rust-rewrite`, `embedding-eval`,
  `find-network-alignments`, `jax-pytorch-rewrite`,
  `kubernetes-rust-rewrite`, `nextjs-vite-rewrite`, `parameter-golf`,
  `post-train-ifeval-gpu`, `ruby-rust-port`, `rust-c-compiler`,
  `rust-java-lsp`, `trimul-cuda`, `vliw-kernel-optimization`, `wasm-simd`,
  `zstd-decoder`.
  - Most build in a public environment, then switch the agent to an allowlist
    and the verifier to no network.
  - `ruby-rust-port` and `rust-java-lsp` instead inherit a minimal Cargo/Rust
    build allowlist across phases. They still count as restricted, not open.

Five tasks require a GPU on Modal: `embedding-eval` (H100),
`jax-pytorch-rewrite` (A100), `parameter-golf` (H100),
`post-train-ifeval-gpu` (H100), and `trimul-cuda` (H100). In v1.1,
`post-train-ifeval-gpu` replaces the old `post-train-ifeval` task; do not submit
or export new trials under the superseded name.

This classification is an operational snapshot, not a second manifest. Before
each eval, diff it against `tasks/dataset.toml` and `scripts/run-benchmark.sh`,
then inspect the selected task's `task.toml` for its current CPU, RAM, storage,
GPU, timeout, and phase-specific network settings.

## 2. Submit

```bash
oddish run -p <task-dir> --agent <agent> --model <provider/model> \
  --n-trials <N> --experiment <exp-id-or-name> -e modal --json \
  [--override-memory-mb <MB>] [--agent-kwarg k=v] [--ae ENV=VAL]
```

- Always `-e modal`. First `--experiment <name>` submission creates the
  experiment; reuse the id (e.g. `a52b8b51`) afterwards.
- `--ae ODDISH_EVAL_NONCE=$(date +%s%N)` is a harmless distinct env var.
- Closed-internet tasks are handled automatically: Oddish infers the model
  API host and disables server-side web tools for restricted agent phases.
  Optional overrides still exist (`--allow-agent-host`, `--disable-web-tools`)
  if you need an extra host or want to force the web-tool disable.

## 3. Load-bearing gotchas (each cost real debugging time)

1. **Sweeps are TARGET-based, not incremental.** `--n-trials N` means "ensure N
   total trials exist for this `(task, agent, model, experiment)`"; it creates
   `N − existing`. Submitting `N ≤ existing` adds **0** (this — not idempotency —
   is the usual reason a rerun shows `added=0`). To add more, raise `N` above the
   current count, or delete some first. (There is also a real 24h idempotency
   key = SHA-256 of the whole sweep payload; a different payload → different key.)

2. **Closed-internet tasks are automatic on Modal/Daytona.** Oddish's Harbor
   pin uses upstream dynamic network policy, so phase switching (`public` env →
   `allowlist` agent → `no-network` verifier) works. At trial time Oddish also
   injects the inferred model host and disables web tools for that restricted
   agent phase — no extra CLI flags required for the common case. Use
   `--allow-agent-host` only for additional hosts (registries, apt mirrors)
   that the task allowlist does not already cover.

3. **OOM (`exit 137`).** High reasoning effort (e.g. `reasoning_effort=xhigh`) +
   heavy build/test/training commands blow past default RAM → container
   OOM-killed. It surfaces as a job `status=success` with `reward=0` and an
   `error_message` of `Command failed (exit 137)`. **Fix:** bump memory, e.g.
   `--override-memory-mb 98304` (96 GB) — this eliminated the 137s. Note a task's
   own `task.toml` default (even 64 GB) can be too low under `xhigh`.

4. **Agent self-kill (`exit 143`).** `mini-swe-agent` runs `pkill -f <keyword>`
   to restart servers/processes it launches (`pkill -f vite`, `pkill -f rj-rust`,
   `pkill -f train_gpt.py`). Harbor put the **task prompt on argv**
   (`mini-swe-agent --task='<prompt>'`), and the prompt text contains those
   keywords, so `pkill -f` matched the agent's own process → SIGTERM → `exit
   143`, heavily on server/build tasks. **Fixed in the meta route (PR #691):**
   deliver the task via the mini-swe-agent config (`run.task`) and strip `--task`
   from argv, so the prompt is no longer on the cmdline. General lesson: any
   agent that shells out `pkill/kill` can self-terminate; keep the prompt off
   argv.

5. **Classify by underlying error, not job status.** Infra failures (137/143/1)
   frequently show as `status=success, reward=0` with a populated
   `error_message`. Always audit `error_message`; treat `AgentTimeoutError` /
   `VerifierTimeoutError` as acceptable and everything else (`Command failed
   (exit N)`, `ExceptionGroup`) as infra to delete + rerun.

6. **CUA verifiers.** Throttle to **≤ ~10 concurrent** CUA trials so the browser
   verifier (Anthropic key) isn't overloaded; confirm the verifier ran cleanly on
   the first CUA completion before scaling. Beware: if the shared platform
   `ANTHROPIC_API_KEY` is quota-capped, CUA verifiers fail independently of the
   agent.

7. **Per-vendor routing / knobs.**
   - `grok-build` (xAI): pass `--agent-kwarg api_backend=chat_completions` when
     the model only serves `/v1/chat/completions`; grok's rich trajectory comes
     from its on-disk session store (captured by the Oddish wrapper), not the
     headless stream.
   - `claude-code`: routes to the direct Anthropic API vs Bedrock via
     `settings.claude_code_force_direct_api` / `ODDISH_CLAUDE_CODE_FORCE_DIRECT_API`
     (a worker/deploy env var, not a CLI flag). Bedrock creds must actually work.
   - `mini-swe-agent` + `meta/`: LiteLLM `openai/` provider → needs
     `OPENAI_API_KEY` (surfaced from `${META_API_KEY}`); `reasoning_effort=xhigh`
     via `--agent-kwarg` (forwarded as `model.model_kwargs.extra_body.reasoning_effort`).
   - Vendor guides often say "keep sampling params default" — honor that except
     for a param you're explicitly asked to set (e.g. `reasoning_effort`). The
     required `x-session-id` header is set by the meta route automatically.

8. **mini-swe-agent + litellm proxy deps (`fastapi`/`orjson`).** The tool is
   installed unpinned (`uv tool install mini-swe-agent`), so a fresh install can
   pull a litellm (>= 1.92) that lazily imports proxy/MCP handlers needing
   `fastapi`, `orjson`, … on the tool-calling completion path
   (`completion(tools=[BASH_TOOL])`). Those aren't in the base tool venv, so the
   first model call dies with `ModuleNotFoundError` and the trial fails fast
   (2 steps, 0 tokens). `OddishMetaMiniSweAgent.install()` reinstalls with the
   proxy extras (`uv tool install mini-swe-agent --with 'litellm[proxy]'`, PR
   #693). If a *new* mini-swe-agent/litellm release breaks imports again, pin the
   version or extend the `--with` set.

## 4. Workflow (breadth → depth → babysit)

1. **Validate** with 1 trial each on representative classes: open non-CUA
   (`stripe-clone`), CUA (`slack-clone`), phase-restricted
   (`zstd-decoder`), inherited build-allowlist (`ruby-rust-port`), and GPU
   (`post-train-ifeval-gpu`). Confirm: auth works (tokens climbing),
   trajectory captured, restricted-network setup succeeds, the requested GPU
   is provisioned, and the CUA verifier runs.
2. **Breadth** across all 20 at a small N.
3. **Depth:** the per-eval target `N` is usually **5**, but confirm it per
   request — vendors sometimes ask for more (e.g. this Meta run used **N=8**).
   Raise the sweep target to `N` plus a **small** buffer (e.g. `N+1`/`N+2`) to
   absorb the occasional stochastic bad trial without over-provisioning.
4. **Babysit loop:** audit by underlying error → delete the infra **and
   degenerate** trials → re-topup (raise target) → repeat until every task has
   ≥ `N` good trials and 0 infra. Then prune each task to exactly `N` (see below).

## 4b. Pruning to N and rerunning degenerate trials

**Degenerate trials — delete and rerun (don't keep).** Beyond the hard infra
failures (137/143/exit-1), also drop trials that aren't a *real* attempt and
rerun them:

- Very few steps/messages relative to the task — e.g. `total_steps < ~15` when
  the task normally runs 60–400 steps (spot them via the step distribution in
  the audit). Watch for a low step count paired with a long runtime / timeout
  (the agent stalled).
- Obvious rate-limit / `429` / `RateLimitError` in the logs, or a 0-token quick
  exit.

**Pruning down to N — keep it light.** When a task has more than `N` good
trials, prune the extras keeping the best, in this priority order:

1. Exclude anything infra or degenerate first.
2. Prefer `reward=1` over `reward=0`.
3. Break ties toward **longer trajectories** (more steps).

**pass@k caveat (important):** do **not** over-optimize this selection —
aggressively dropping low-reward trials to keep the "best" biases the pass@k /
pass@1 statistics the eval reports. Keep buffers small so you only ever prune a
*handful* per task (that's fine); systematically discarding many valid trials to
inflate scores is not. Tasks that already sit at exactly `N` need no pruning at
all (zero bias). The clean way to minimize bias is a small buffer (target
`N+1`/`N+2`) + reruns of only infra/degenerate trials, rather than a big
over-provision followed by heavy pruning.

## 5. Monitor / audit

- `oddish status <task_id> --json` → per trial: `status`, `reward`,
  `total_steps`, `has_trajectory`, `input/output/cache_tokens`, `cost_usd`,
  `error_message`, `jobs[].attempts`, `task_version`, `experiment_id`.
- `oddish pull <trial_id> --logs --files [--structured]` → verify the ATIF
  `trajectory.json` has steps with `tool_calls` + `observation` + token
  `metrics`, plus `verifier/reward.txt` and `verifier/metrics.json`.
- `oddish cancel <task_id>` stops in-flight; `oddish delete -t <id> …`
  (admin) removes trials (soft delete).

## 6. Export to the SWE-Marathon logs bucket (`ralphbench-logs`)

`scripts/read-swe-marathon-logs.py` (in `swe-marathon`) documents the layout.
Bucket `ralphbench-logs` (region `us-west-2`); per task:
`<task>/_manifest.json` + `<task>/<trial_id>/{config.json, result.json,
<source_trial_name>/{agent/, verifier/}}` where `source_trial_name` is the
**completed** attempt dir (the one with `verifier/reward.txt`).

- **Merge** into the existing `_manifest.json` (never overwrite): add each trial
  under `trials` (`agent`, `model`, `provider`, `origin`, `reward`, tokens,
  `cost_usd`, `started/finished_at`, `task_version_id`, `source_trial_name`,
  `artifact_layout="harbor-nested"`, `artifact_prefix`), then update `n_trials`,
  `pair_counts` (`"agent|model"`), `n_pass`, `task_versions`, and append a `note`.
- Store the **public** model name if the eval used an internal codename
  (relabel every text file, e.g. `xai/v9-stickynote` → `grok-4.5`; scan all
  uploaded files afterward to confirm no codename leaked).
- A brand-new task gets its **own prefix + fresh manifest** — do not merge into a
  superseded task's prefix.
- Match existing conventions: models are `provider/model`; `nop`/`oracle`
  baselines use `model="default"`, `provider="default"`, `has_trajectory=false`.
- The AWS creds for this bucket are short-lived STS (~1h) — persist to an env
  file, verify with `sts get-caller-identity`, and expect to refresh mid-upload
  on large exports. `boto3` is available in the `oddish` venv (no `aws` CLI
  needed).
