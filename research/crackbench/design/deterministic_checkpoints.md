# Deterministic Checkpoints for Long-Horizon Cyber Tasks

This is a **design reference**. The auto-research harness feeds every file in this
directory to a fleet of Haiku "design-reader" subagents. Each subagent distills
the material below into a compact set of *deterministic checkpoint* rules, and the
merged result is injected into the Fable task-generation prompt.

To steer generation with your own design (e.g. an internal spec), drop additional
`.md` / `.txt` files next to this one — the readers pick them up automatically.

---

## 1. What a checkpoint is

A **checkpoint** is a single, independently verifiable sub-goal of a task. It has:

- a stable `id` (e.g. `cp-unpack`),
- a human `title` and `description`,
- a **deterministic `verify_cmd`**: a shell command that exits `0` iff the
  checkpoint is satisfied, and non-zero otherwise,
- a `weight` (its share of the dense reward),
- a reserved `depends_on` list naming the checkpoints that must be reached first.
  The harness records `depends_on` but does **not** yet build or enforce the DAG —
  that is deliberately deferred. Keep the edges honest anyway so the graph can be
  turned on later without regenerating tasks.

A task's reward is the weighted fraction of checkpoints whose `verify_cmd` passes.
That gives **dense, partial-credit grading** instead of a single pass/fail bit,
which is what makes long-horizon progress measurable.

## 2. Determinism is the whole point (the CrackMeBench lesson)

CrackMeBench scores agents against **executable oracles**, not free-form
explanations. Adopt the same discipline for every checkpoint:

- The verifier must reach a verdict with **no model, no network, and no human**.
  A checkpoint graded by "ask an LLM if this looks right" is not deterministic and
  is out of scope.
- Prefer checks the environment can settle by itself: a produced artifact hashes to
  a known value, a recovered password authenticates against the real binary, a
  patched binary now exits `0`, a written file contains an exact recovered secret,
  a service answers a probe on a port.
- Keep binaries **symbol-poor** and solutions **externally scored**: the agent
  submits an answer/artifact; `verify_cmd` checks it against ground truth that the
  agent never sees.
- Every checkpoint must be reachable from the shipped `solution/` alone. If the
  reference solution cannot trip a checkpoint, neither can an agent, and the task
  is broken.

## 3. Designing for the long horizon (≥ 30 minutes)

A task is "long-horizon" when a strong baseline solver either **fails** it or needs
**≥ 30 minutes** of wall-clock. Checkpoints are the main lever for getting there:

- **Chain irreversible stages.** Later checkpoints should require the *output* of
  earlier ones (unpack → locate the check routine → recover the key → forge a valid
  license → prove it). Depth, not breadth, is what burns solver time.
- **Make each stage individually cheap to verify but expensive to reach.** The
  `verify_cmd` should be a couple of lines; the *work* to make it pass should be
  substantial.
- **Add friction that resists shortcuts**, not friction that resists *starting*:
  anti-debugging, packing/self-modifying code, stripped symbols, custom VMs and
  bytecode, layered/rolled crypto, decoy paths, and state that must be reconstructed
  rather than dumped. Avoid artificial time-wasting (huge brute-force spaces with no
  insight) — that lengthens runtime without testing capability.
- **6–10 checkpoints** is a good band for a 30–90 minute task. Fewer than 3 rarely
  clears the bar; more than ~12 becomes bookkeeping.
- **Front-load a cheap checkpoint or two** so partial-credit signal appears early
  and a stuck agent still produces a gradient.

## 4. Verifier hygiene (Harbor conventions)

The generated `tests/test.sh` runs each `verify_cmd`, sums the passed weights, and
writes:

- `/logs/verifier/reward.txt` — the dense reward in `[0, 1]`.
- `/logs/verifier/metrics.json` — `{"schema_version": 1, ...}` with per-checkpoint
  pass/fail, so the run is inspectable.
- `/logs/verifier/ctrf.json` — CTRF counts (`passed`/`failed`/`total`) so dashboards
  render a test line.

Rules that keep tasks trustworthy:

- **No side effects in `verify_cmd`.** Checks read state; they never create the very
  artifact they test for. A check that also does the work grades itself.
- **Idempotent and order-independent** within an iteration: running the verifier
  twice yields the same reward, and one checkpoint's check must not depend on another
  check having run (dependencies live in the task state, not in the verifier's own
  execution order).
- **Fail closed.** A missing artifact, a malformed submission, or a crashed check is
  a non-zero exit (checkpoint not met), never a pass.
- **Pin the ground truth in `solution/`**, not in `instruction.md`. The agent reads
  the instruction; it must not be able to read the answer.

## 5. Anti-patterns to reject

- Checkpoints whose `verify_cmd` greps the agent's *chat/log* instead of the
  environment state.
- "Explain how X works" goals with no executable check.
- Rewards that hinge on wall-clock, randomness, or network reachability.
- A single monolithic checkpoint (all-or-nothing) — it destroys the dense signal and
  hides where solvers actually stall.
- Tasks that are long only because of an unavoidable brute-force, with no reasoning
  content.

## 6. Checklist the generator should satisfy per task

1. 6–10 deterministic checkpoints, each with a runnable `verify_cmd` and a weight.
2. At least one hard dependency chain ≥ 3 deep encoded in `depends_on`.
3. Ground truth lives only in `solution/`; `instruction.md` states the goal and rules.
4. No-network, no-LLM, self-contained Docker environment with standard tooling.
5. A reference solution that trips every checkpoint.
6. A plausible reason the task needs ≥ 30 minutes that is *reasoning* work, not busywork.
