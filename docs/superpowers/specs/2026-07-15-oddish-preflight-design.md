# Oddish preflight — task checks before trials

**Date:** 2026-07-15
**Status:** Approved, pending implementation

## Problem

`oddish run` will happily upload and submit a task directory that is broken,
leaky, or self-defeating. Today's only gate is `validate_tasks()`
(`oddish/src/oddish/cli/api.py:110`), which loads Harbor's `Task(path)` and
reports whatever exception it raises, stringified as
`f"{type(e).__name__}: {e}"`. There is no "preflight this dir and explain what's
wrong" helper. Trials cost money and GPU time; a task that was never going to
produce a meaningful result should fail before submission, not after.

Separately, a task farmed from an upstream PR can hand the agent the answer. If
the image ships a `.git` directory, the agent runs `git log` and reads the fix
commit. If the Dockerfile clones the upstream repo, same outcome by a different
route. Nothing currently catches this.

`abundant-ai/harbor-lh` has 18 CI checks (`ci_checks/*.sh`) that encode much of
this knowledge, but they run only in that repo's GitHub Actions, only on PRs,
and only against `tasks/**`. They are invisible to anyone running `oddish run`
anywhere else.

## Goals

- Gate `oddish run` on task validity *before* upload, like a pre-commit hook
  gates a commit.
- Port the generally-applicable subset of harbor-lh's checks into oddish so any
  task repo benefits, not just harbor-lh.
- Add a net-new check for repo/branch-history exposure, which harbor-lh does not
  have.
- Stay overridable: a false positive must never be able to wedge an urgent run.

## Non-goals

- Porting harbor-lh's house-policy checks (see "Check triage" below).
- A config layer for check thresholds. YAGNI until a second repo wants different
  knobs.
- Migrating harbor-lh's CI to call `oddish preflight`. That is the eventual
  end-state this design deliberately enables, but it is out of scope here.

## Check triage

The 18 harbor-lh checks split three ways. The dividing line is **Harbor
semantics** (true for any task, anywhere) versus **harbor-lh house policy**
(hardcoded constants reflecting one fleet's choices).

### Generally applicable (11)

`check-dockerfile-references`, `check-task-absolute-path`,
`check-test-file-references`, `check-test-sh-sanity`, `check-reward-format`,
`check-metrics-partial-score`, `check-artifacts`, `check-anti-cheat-soundness`,
`check-asset-encryption`, `check-closed-internet`, `check-solution-format`.

### House policy — not ported

- `check-dockerfile-base-image` — hardcodes `FROM ubuntu:24.04`.
- `check-timer` — requires harbor-lh's `environment/timer.sh` convention.
- `check-task-resources` — floors `cpus >= 4`, `memory_mb >= 16384`,
  `storage_mb >= 16384`.
- `check-dockerfile-sanity` — bans pinned apt versions, which is arguably
  *anti*-reproducibility and is harbor-lh's call to make, not oddish's.
- `check-task-fields` — requires harbor-lh's metadata list
  (`difficulty_explanation`, `solution_explanation`, …).

These are not wrong; they are parameterized in spirit but hardcoded in fact.
Porting them means designing a config layer, which this spec declines to do.

### Not portable

`check-programbench-overlap`, `check-similarity`, `check-ai-generated` — tied to
harbor-lh's dataset and task-farming pipeline.

## Scope of this change

Four checks ship: three ported, one net-new.

| Check | Origin | What it does | Cost to port |
| --- | --- | --- | --- |
| `closed_internet` | `check-closed-internet.sh` | Fails any phase whose effective `network_mode` resolves to `public` (including implicitly, and including a separate verifier container or a per-step override) without a ≥20-char justification | resolves via Harbor's own `resolve_trial_network_plan` |
| `anti_cheat_soundness` | `_anti_cheat_scan.py` | Flags brittle source-scanning anti-cheat regexes | near-verbatim; already Python |
| `solution_format` | `check-solution-format.sh` | Fails `.patch`/`.diff` in `solution/` and patch-application in `solve.sh` | ~40 lines |
| `provenance` | net-new | Build-time repo fetches, and `.git` inside the build context | the real work |

### Why `dockerfile_leaks` is not here

An earlier draft of this spec included a port of
`check-dockerfile-references.sh` — renamed `dockerfile_leaks` — to stop a task
Dockerfile from baking the solution or the grader into the agent's image. It was
implemented, reviewed three times, and then **removed**, because the leak it
guards is already impossible.

Harbor builds the task image with the build context set to `environment/`
(`harbor/environments/docker/docker.py:240`), and `tests/` and `solution/` are
*siblings* of `environment/`, not children. A task Dockerfile cannot `COPY` them:
they lie outside the build context and Docker refuses. `COPY tests/ /app/`
resolves to `environment/tests/`, which is not the real grader; `COPY ../tests`
fails the build outright.

harbor-lh's original has the same property, which is likely why it was only ever
a loose literal grep — it was never load-bearing. Broadening it to catch
directory and glob forms only produced ERROR-severity false positives on ordinary
Dockerfile idioms (`FROM node:20 AS tests`, `RUN echo "all tests pass"`).

The real leak vector in this family is a task author *duplicating* the grader
inside `environment/`, where it would legitimately be copied in. Catching that
requires comparing content (e.g. hashing `environment/**` against `tests/**`),
not grepping the Dockerfile. That is a genuinely useful check and a candidate
follow-up, but it is a different design and out of scope for v1.

The lesson generalizes and is why the `.git` rule below is scoped to
`environment/`: **only what is inside the build context can enter the image.**

### Deferred

The remaining general checks land later behind the same registry:
`test_file_references`, `test_sh_sanity`, `reward_format`,
`metrics_partial_score`, `artifacts`, `asset_encryption`, and
`task_absolute_path`.

`task_absolute_path` is deferred on cost/value grounds, not by oversight.
Despite its name it does not detect host-path leakage. It derives `WORKDIR` from
the Dockerfile (defaulting to `/app`) and fails `instruction.md` for using
*relative* paths (`data.csv`, `./out.txt`, `scripts/run.sh`) — it *wants*
absolute paths so the agent's cwd never matters. That is a task-clarity concern
with no integrity dimension, and it costs ~100 lines of stacked regex over a
60-extension allowlist: the most expensive check to port and the least aligned
with v1's theme.

The others are expensive too — `check-test-file-references.sh` is 9.5KB of bash,
`check-asset-encryption.sh` 8.9KB — and port bugs hide in exactly that kind of
code.

## Architecture

New package `oddish/src/oddish/preflight/`:

```
preflight/
  registry.py      # CHECKS: list[Check]; each = id, description, fn
  models.py        # Finding, Severity, Check
  runner.py        # run_checks(paths) -> list[Finding]
  checks/
    closed_internet.py
    solution_format.py
    anti_cheat_soundness.py
    provenance.py
```

Each check is a pure function `(task_dir: Path, config: TaskConfig) ->
list[Finding]`. Checks do not print and do not exit. Rendering and exit codes
live at the CLI edge.

This split is load-bearing: it is what lets the same check bodies serve three
callers — the `oddish preflight` subcommand, the auto-run inside `oddish run`,
and eventually harbor-lh's CI shelling out to `oddish preflight --json`.

Checks receive Harbor's already-parsed `TaskConfig` (`cli/api.py:340`) rather
than re-parsing `task.toml`. Re-parsing would drift from Harbor's schema the
first time it changes.

### Models

```python
class Severity(StrEnum):
    ERROR = "error"   # blocks the run
    WARN = "warn"     # printed, does not block

@dataclass(frozen=True)
class Finding:
    check_id: str
    severity: Severity
    task_dir: Path
    message: str
    path: Path | None = None      # file the finding is in
    line: int | None = None       # 1-indexed
    fix_hint: str | None = None
```

### Severity assignment

| Finding | Severity |
| --- | --- |
| Finding | Check | Severity |
| --- | --- | --- |
| Unsuppressed repo fetch in `environment/` (Dockerfile or `*.sh`) | `provenance` | error |
| `.git` inside `environment/` (the build context) | `provenance` | error |
| No `.git` in `environment/`, but no `.dockerignore` excluding it | `provenance` | warn |
| Any phase resolves to public with no justification | `closed_internet` | error |
| Justification present but under 20 chars, or not a string | `closed_internet` | error |
| `.patch` / `.diff` in `solution/` | `solution_format` | error |
| `solve.sh` applies a patch (`git apply` / `patch -p`) | `solution_format` | error |
| Brittle anti-cheat regex (unsuppressed) | `anti_cheat_soundness` | error |

`warn` exists so that "you have no `.dockerignore`" is not shouted with the same
volume as "your image contains the answer."

## The provenance check

Two rules.

### Fetch rule

Scoped to the Docker build context (`environment/`), the only place a fetch can
bake into the agent's image. Scan `environment/Dockerfile` and every `*.sh`
under `environment/` for:

- `git clone`, `git fetch`
- `pip install git+…`
- archive URLs: `*/archive/*.tar.gz`, `codeload.github.com`, release tarballs

It deliberately does **not** scan `solution/solve.sh` or `tests/test.sh`. Harbor
runs those in the oracle and verify phases, which execute after and outside the
agent phase (`_run_agent()` completes before `_run_verifier()` in
`trial/single_step.py`), so a fetch there never reaches the agent. A fetch in an
`environment/` build script (e.g. `RUN ./setup.sh` where `setup.sh` clones the
upstream) *does* reach the agent and is caught — the direct `RUN git clone` and
the indirect script both live in the build context.

Every hit is an error **unless** its line carries a suppression comment:

```
RUN git clone --depth 1 https://github.com/foo/dep  # provenance-ok: pinned third-party dep, not the task upstream
```

The reason text is required and must be at least 10 characters.

Rationale for flag-everything-with-an-escape-hatch over the alternatives: a
regex cannot distinguish cloning the task's own upstream repo (fatal — hands the
agent the fix commit) from cloning an unrelated pinned dependency (routine).
Comparing against a declared `[metadata] source_repo` would be precise but
silently no-ops on any task that omits the field — exactly the task most likely
to be careless. Flagging everything has zero false-negatives; the cost is a
one-time comment on legitimate clones.

The `# provenance-ok:` grammar deliberately mirrors harbor-lh's existing
`# anti-cheat-ok:` idiom so task authors learn one suppression form, not two.

### `.git` rule

Scoped to `environment/` — the Docker build context
(`harbor/environments/docker/docker.py:240`). Only what lives inside the build
context can enter the image, so only a `.git` there can reach the agent.

- **Error** if a `.git` exists anywhere under `environment/` and no
  `.dockerignore` excludes it.
- **Warn** if no `.git` is present under `environment/` and no `.dockerignore`
  excludes it — the leak is latent rather than live, and a future `COPY` of a
  git checkout would go unnoticed.

Any `.git` elsewhere in the task directory is ignored: Docker refuses paths
outside the build context, so it provably cannot be baked in. Flagging it would
be an ERROR-severity false positive on something that cannot happen — the same
mistake that got `dockerfile_leaks` removed.

This is the quietest leak of the ones that remain reachable: the agent runs
`git log` in `/app` and reads the fix commit message, or `git diff HEAD~1` and
reads the answer outright.

### Out of scope for provenance

- Git remotes / reflog reachable at runtime. Covered in practice by the fetch
  rule plus `closed_internet`.
- `allow_internet = true` — already covered by the ported `closed_internet`
  check; duplicating it here would double-report.

## CLI surface

### `oddish preflight <path>... [--json]`

Runs the registry, renders findings, exits 0 (clean or warn-only) or 1 (any
error). `--json` emits findings via the existing `print_json()`
(`cli/config.py`), for CI consumers:

```json
{
  "ok": false,
  "findings": [
    {
      "check_id": "provenance",
      "severity": "error",
      "task_dir": "tasks/foo",
      "path": "tasks/foo/environment/Dockerfile",
      "line": 12,
      "message": "git clone of upstream repo exposes branch history to the agent",
      "fix_hint": "Vendor the source at a pinned revision, or suppress with `# provenance-ok: <reason>`"
    }
  ]
}
```

`--json` is cheap now and annoying to retrofit; it is also the precondition for
harbor-lh ever rendering an oddish-driven sticky comment.

### `oddish run --force`

`run` calls the same preflight entry after task resolution
(`cli/run.py:800-828`) and before upload (`cli/run.py:934`). The gate sits on the
seam before the irreversible, costly step.

Any error-severity finding on any task aborts the run. `--force` proceeds anyway
but **still prints every finding, downgraded to yellow warnings**. Skipping the
gate must not mean skipping the information.

`--force` is the repo's established idiom for this (`cli/cancel.py:54`,
`cli/backfill_analysis.py:42`), and is currently unused in `run.py`.

`--force` also derisks the strict default: the failure mode of a preflight gate
is not a false-negative, it is a false-positive wedging an urgent run. The
flag-every-fetch rule is precisely the one most likely to misfire. `--force`
turns that from an outage into an annoyance.

## Behavior change

Preflight aborts if **any** task fails. Today `validate_tasks()`
(`cli/api.py:110`) exits 1 only if *every* task fails; one broken task in a sweep
of twenty prints `✗` and is silently dropped while the run proceeds.

Anyone relying on "throw twenty dirs at it, run whichever are valid" will now
need `--force`. This is intentional — silently not running a task you asked for
is worse than a loud stop — but it is a real change and belongs in `CHANGELOG.md`,
not the fine print.

## Error rendering

Follows the existing convention: `error_console.print("[red]...[/red]")` from
`cli/config.py`, `[yellow]` for warnings, `[dim]` for asides, then
`raise typer.Exit(1)`. Findings render grouped by task dir, then by check, with
`file:line` prefixes so they are clickable.

## Testing

- Table-driven per check: a `tmp_path` task dir with one passing and one failing
  fixture each.
- Promote `_write_minimal_task` (`tests/test_cli_upload.py:15`) into
  `tests/conftest.py` as a shared fixture. Preflight is its third caller, so it
  has earned promotion.
- `CliRunner` tests for `oddish preflight` exit codes and `--json` shape.
- A `run --force` test asserting it proceeds *and* still prints findings.
- Provenance suppression tests: reason too short, missing reason, valid
  suppression.

## Open questions

None. Config layer, remaining six checks, and the harbor-lh CI inversion are
deliberate follow-ups.
