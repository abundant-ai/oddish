# crackbench — auto-research harness for long-horizon cyber tasks

A small research harness that **generates hard cybersecurity benchmark tasks**,
in the style of [CrackMeBench](https://arxiv.org/abs/2605.10597) (binary reverse
engineering for agents), and keeps only the ones that are **long-horizon** — a
strong baseline solver either fails them or spends **≥ 30 minutes**.

It follows an auto-research loop. Each iteration starts from a clean slate:

```
┌─ iteration (fresh subagents, no carried context) ─────────────────────────┐
│  1. Haiku design-reader subagents  →  read design/*.md → deterministic     │
│     (one per design doc, fanned out)   checkpoint rules                     │
│  2. Fable generator subagent       →  5 CrackMeBench-style tasks, each      │
│                                        built around those checkpoints       │
│  3. "Brock" baseline solver        →  solve time + reward per task          │
│  4. gate: ≥ 2 of the 5 are long-horizon (solver fails OR ≥ 30 min)          │
└────────────────────────────────────────────────────────────────────────────┘
     ↑ repeat: clear all subagent context, start from scratch
```

Long-horizon tasks are accumulated into an accepted corpus and materialized as
Harbor task directories (the format `oddish run` consumes).

## Quickstart (offline, zero setup)

Runs with a deterministic fake LLM and a mock solver — no API key, no network, no
oddish install:

```bash
cd research/crackbench
PYTHONPATH=. python3 -m crackbench run --iterations 3 --out ./runs
```

You get, under `./runs/`: `summary.json`, `summary.md`, one `iteration-NN.json`
per round, and `accepted/<slug>/` — a full Harbor task dir for every long-horizon
task (with `task.toml`, `instruction.md`, `environment/Dockerfile`, `solution/`,
and a dense-reward `tests/test.sh`).

Run the tests:

```bash
pip install pytest        # or: pip install -e '.[dev]'
PYTHONPATH=. python3 -m pytest -q
```

## Live mode (real subagents) and real solve time

```bash
pip install -e '.[live]'          # installs the anthropic SDK
export ANTHROPIC_API_KEY=sk-...

# Real Fable + Haiku subagents, mock solver (cheap, no task execution):
PYTHONPATH=. python3 -m crackbench run --live --iterations 3

# Real subagents AND real solve time measured via oddish (needs ODDISH_API_KEY):
export ODDISH_API_KEY=ok_...
PYTHONPATH=. python3 -m crackbench run --live --solver oddish \
    --solver-agent claude-code --solver-model anthropic/claude-sonnet-4-5
```

`--solver oddish` materializes each candidate, submits it with `oddish run`, waits
for the trial, and reads back `trajectory_duration_seconds` and reward — the
authoritative long-horizon measurement. It is best-effort until validated against
a live deployment (JSON field discovery is defensive).

### Key options

| Flag | Default | Meaning |
|------|---------|---------|
| `--iterations` | 5 | max iterations |
| `--per-iteration` | 5 | tasks generated per iteration |
| `--min-long-horizon` | 2 | gate: min long-horizon tasks per iteration |
| `--minutes` | 30 | long-horizon solve-time threshold |
| `--target` | none | stop once N long-horizon tasks are collected |
| `--design-dir` | `./design` | design docs the Haiku readers consume |
| `--live` | off | real Anthropic API vs offline fake |
| `--solver` | `mock` | `mock` (heuristic) or `oddish` (real solve time) |
| `--cache-checkpoints` | off | read design docs once instead of every iteration |
| `--seed` | 0 | reproducibility |

## How the request maps to this code

Some terms in the original request were ambiguous; here is exactly how each was
interpreted, and where it lives so you can adjust it.

- **"Follow CrackMeBench Online"** → tasks adopt CrackMeBench's discipline:
  deterministic **executable oracles**, symbol-poor challenges, no-network Docker
  sandbox, externally-scored submissions. Encoded in the generator prompt
  (`subagents.py`) and the design reference (`design/deterministic_checkpoints.md`).
- **"Fable subagent that generates 5 tasks"** → one Fable generator subagent per
  iteration returns a batch of 5 (`subagents.generate_tasks`, model `claude-fable-5`).
- **"2 of 5 long-horizon"** → the per-iteration gate (`min_long_horizon=2`,
  `tasks_per_iteration=5`), configurable.
- **"long-horizon = fails or ≥ 30 min on the solver"** → `solver.classify_long_horizon`.
  30 min is also oddish's own `PROBE_AGENT_TIMEOUT_SEC` default.
- **"on Brock"** → **Brock is the baseline solver.** It is pluggable: `MockSolver`
  (offline, seeded difficulty heuristic) and `OddishSolver` (real solve time). Point
  it at whatever agent/model you consider the reference solver via `--solver-agent` /
  `--solver-model`.
- **"clear all subagent context each iteration, start from scratch"** → every
  iteration builds a fresh subagent transport and re-derives checkpoints; no state is
  threaded between rounds (`harness.run_iteration`). This is deliberate: it makes each
  batch an independent sample, so the 2-of-5 gate is an honest base-rate acceptance
  test of the prompt rather than a number inflated by a generator repeating one lucky
  task.
- **"Use Haiku subagents to read through the design for these checkpoints"** → the
  Haiku design-readers (`subagents.read_checkpoint_guidance`, model
  `claude-haiku-4-5`) fan out over every file in `--design-dir`. The bundled
  `design/deterministic_checkpoints.md` is a working reference; **drop your own design
  doc(s) into `design/`** and the readers pick them up automatically. (The repo's
  `fvsmith` is only an example task name, not a design doc, so the design source was
  made a directory you populate.)
- **"deterministic checkpoints"** → every task is built from checkpoints with a shell
  `verify_cmd` that exits 0 iff met; the materialized `tests/test.sh` runs them and
  writes a **dense, weighted reward** plus `metrics.json` / `ctrf.json`.
- **"we don't need the DAG graph yet"** → checkpoints carry a `depends_on` list that
  is **recorded but not enforced**. The verifier scores checkpoints independently; the
  dependency graph is captured so it can be turned on later without regenerating tasks.

## File tour

```
crackbench/
├── config.py       HarnessConfig — all knobs (models, gate, thresholds, io)
├── models.py       Checkpoint / GeneratedTask / SolveResult / *Result dataclasses
├── llm.py          subagent transport: AnthropicLLM (real) + FakeLLM (offline)
├── subagents.py    Haiku design-readers + the Fable generator, and their prompts
├── solver.py       Brock: MockSolver + OddishSolver + long-horizon classifier
├── materialize.py  GeneratedTask → Harbor task dir + dense-reward verifier
├── harness.py      the loop (fresh context per iteration, gate, artifacts)
└── cli.py          `python -m crackbench run ...`
design/             design docs the Haiku readers distill (add your own here)
tests/              offline end-to-end tests (fake LLM + mock solver)
```

## Extending

- **Steer generation**: edit `design/deterministic_checkpoints.md` or add files to
  `design/`. No code change needed.
- **Different reference solver**: pass `--solver oddish --solver-agent/--solver-model`,
  or implement the `Solver` protocol in `solver.py`.
- **The DAG (next step)**: `Checkpoint.depends_on` is already populated; add a graph
  builder + a verifier that gates dependent checkpoints on their prerequisites.

## Status / caveats

- Offline mode is fully runnable and tested; it demonstrates the loop and materializes
  valid Harbor task scaffolds.
- The generated `environment/` is a **scaffold** — full task realization (shipping the
  real symbol-poor binary and its oracle) is intentionally deferred alongside the DAG.
  The verifier structure and dense reward are real; the challenge artifacts are the
  `TODO(full-realization)` markers.
- `OddishSolver` needs a reachable oddish deployment and is best-effort until run
  against one.
