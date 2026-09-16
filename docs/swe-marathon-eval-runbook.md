# Runbook: full vendor-model eval on SWE-Marathon (via Oddish, on Modal)

This is the operational playbook for running a full SWE-Marathon eval (e.g. a
vendor asks for **8 trials × 20 tasks = 160 trials** on a new/internal model)
end to end on Oddish + Modal. It captures the gotchas learned running xAI
(`grok-build`), Meta (`mini-swe-agent`), Moonshot (`claude-code`), ZAI (`claude-code`), etc. evals so the next one is fast.

**Target outcome:** the requested number of valid trials per task. Set validity
rules before examining rewards. Preserve failed attempts and exclusion reasons;
replace confirmed infrastructure failures through the supported retry workflow.
A model failing the task or reaching its agent time limit is still an evaluated
attempt. A verifier timeout needs investigation: do not assume it is either a
valid score or an infrastructure failure without checking the task contract.

The task list, resource sizes, provider fixes, and concurrency observations below
are historical examples. Check the current dataset and deployed agent code before
applying them. The [August campaign record](archive/swe-marathon-terra-campaign.md)
records a later campaign-specific browser-verifier cap of 25; neither 10 nor 25
is a universal platform limit.

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

- **Open internet (4):** `excel-clone`, `mastodon-clone`, `s3-clone`, and
  `slack-clone`. All four use a CUA browser-agent verifier on an Anthropic key.
- **Internet restricted (16):** `biofabric-rust-rewrite`, `embedding-eval`,
  `find-network-alignments`, `jax-pytorch-rewrite`,
  `kubernetes-rust-rewrite`, `nextjs-vite-rewrite`, `parameter-golf`,
  `post-train-ifeval-gpu`, `ruby-rust-port`, `rust-c-compiler`,
  `rust-java-lsp`, `stripe-clone`, `trimul-cuda`, `vliw-kernel-optimization`,
  `wasm-simd`, `zstd-decoder`.
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
- Unless the eval request specifies otherwise, use the maximum reasoning effort
  supported by the selected agent and model.
- `--ae ODDISH_EVAL_NONCE=$(date +%s%N)` is a harmless distinct env var.
- Closed-internet tasks are handled automatically: Oddish infers the model
  API host and disables server-side web tools for restricted agent phases.
  Optional overrides still exist (`--allow-agent-host`, `--disable-web-tools`)
  if you need an extra host or want to force the web-tool disable.
- Do not pass `--qa` during the initial submission. Task-scoped QA is admitted
  automatically after the audit and all solver trials settle. The `--qa` flag
  is only valid with `--retry` when deliberately requesting a QA rerun.

## 3. Load-bearing gotchas (each cost real debugging time)

1. **Sweeps are TARGET-based, not incremental.** `--n-trials N` means "ensure N
   total trials exist for this `(task, agent, model, experiment)`"; it creates
   `N − existing`. Submitting `N ≤ existing` adds **0** (this — not idempotency —
   is the usual reason a rerun shows `added=0`). To add more, raise `N` above the
   current count. Use the supported retry workflow for failed attempts.
   (There is also a real 24h idempotency
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
   own `task.toml` default (even 64 GB) can be too low under `xhigh`. Only do this if you actually observe OOM due this reason.

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
   can show as `status=success, reward=0` with a populated `error_message`.
   Inspect the saved result, worker errors, and logs. Exit codes alone do not
   establish infrastructure failure: an agent can terminate its own process or
   exhaust task resources. Apply the validity rules agreed for this evaluation;
   preserve the evidence and retry confirmed infrastructure failures.

6. **CUA verifiers (historical starting point).** Start conservatively (the
   original evaluation used roughly 10 concurrent CUA trials) so the browser
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

1. **Validate** with 1 trial each on representative classes: CUA/open-internet
   (`slack-clone`), restricted non-CUA (`stripe-clone`), phase-restricted
   (`zstd-decoder`), inherited build-allowlist (`ruby-rust-port`), and GPU
   (`post-train-ifeval-gpu`). Confirm: auth works (tokens climbing),
   trajectory captured, restricted-network setup succeeds, the requested GPU
   is provisioned, and the CUA verifier runs.
2. **Breadth** across all 20 at a small N.
3. **Depth:** the per-eval target `N` is usually **8**, but confirm it per
   request because vendor requirements can vary.
   Submit the requested target without selecting extra attempts by reward.
4. **Monitor:** investigate incomplete or failed trials, record confirmed
   infrastructure exclusions, and replace those attempts through retries until
   the agreed sample is complete. Report unresolved failures explicitly.

## 4b. Trial inclusion and replacement

Do not discard a trial solely for a low reward, short trajectory, or agent
timeout. In a reasoning-effort comparison, shorter attempts may be the treatment
effect being measured. Diagnose rate limits, missing credentials, and storage or
verifier failures from evidence rather than step-count thresholds.

Never prefer successful trials or longer trajectories when selecting the final
sample: even selecting the best N from N+1 biases the measured success rate.
Keep all valid trials and report the actual count, or use a selection rule fixed
before observing results (such as the first N submitted valid trials). Record
excluded attempt IDs and reasons without deleting their evidence.

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


## 7. Reusable background-agent prompt

Customize the placeholders below for the requested agent, model, reasoning
settings, trial count, and baseline count. The agent should have access to
`abundant-ai/oddish`, `abundant-ai/harbor`, and `abundant-ai/swe-marathon`.

> Run an evaluation across all 20 tasks in SWE-Marathon using Oddish and Modal.
> Use:
>
> - Agent: `<agent>`
> - Model: `<provider/model>`
> - Reasoning settings: `<reasoning-settings>` (default to the maximum supported
>   effort unless the request specifies otherwise)
> - Target trials per task: `<N>`
> - Nop and oracle baselines per task: `<baseline-N>`
> - Oddish experiment: `<experiment-name-or-ID>`
>
> Use Oddish for all submission, monitoring, and retry operations. SWE-Marathon
> documentation may describe upstream Harbor commands, but Oddish uses its own
> Harbor fork and CLI workflow. Account for those differences and follow this
> runbook as the operational source of truth.
>
> Submit trials on Modal with a command shaped like:
>
> ```bash
> uv run oddish run -p <task-dir> \
>   --agent <agent> \
>   --model <provider/model> \
>   --n-trials <N> \
>   --experiment <experiment-name-or-ID> \
>   -e modal \
>   --json \
>   <additional-options>
> ```
>
> Work breadth-first: start with a small number of trials across all 20 tasks,
> validate each execution class, and only then increase each task to the target
> depth. Keep every run in one Oddish experiment.
>
> Sixteen tasks have restricted internet access. Ensure their model API host is
> allowlisted and server-side web-search tools are disabled during the agent
> phase. Oddish should infer both automatically; verify this behavior rather
> than adding manual overrides by default. Consult the provider documentation
> if its required API hosts are unclear.
>
> The four open-internet tasks use CUA verifiers backed by an Anthropic API key.
> Choose concurrency from current verifier quota and observed failures; historical
> caps are examples. Confirm an initial verifier completes before scaling.
>
> Monitor trial logs and trajectories throughout the run. Trials should have no
> infrastructure failures: an `AgentTimeoutError` is an acceptable terminal
> outcome. Investigate agent process exits, verifier failures, missing credentials,
> rate limits, and Harbor exceptions. Replace only confirmed infrastructure
> failures under the agreed evaluation rules, retaining evidence and reasons.
>
> Also run `<baseline-N>` nop trials and `<baseline-N>` oracle trials per task.
> During this phase, do not prune trials or export artifacts to another bucket.
> Follow the auditing and rerun guidance in
> `oddish/docs/swe-marathon-eval-runbook.md`.
