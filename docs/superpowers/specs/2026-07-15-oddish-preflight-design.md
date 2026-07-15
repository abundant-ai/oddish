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

Six checks ship: five ported, one net-new.

| Check | Origin | Cost to port |
| --- | --- | --- |
| `dockerfile_references` | `check-dockerfile-references.sh` | ~30 lines |
| `task_absolute_path` | `check-task-absolute-path.sh` | ~30 lines |
| `closed_internet` | `check-closed-internet.sh` | ~30 lines, reads `TaskConfig` |
| `solution_format` | `check-solution-format.sh` | ~30 lines |
| `anti_cheat_soundness` | `_anti_cheat_scan.py` | near-free; already Python |
| `provenance` | net-new | the real work |

The remaining six general checks (`test_file_references`, `test_sh_sanity`,
`reward_format`, `metrics_partial_score`, `artifacts`, `asset_encryption`) land
later behind the same registry. They are the expensive ones —
`check-test-file-references.sh` is 9.5KB of bash, `check-asset-encryption.sh`
8.9KB — and port bugs hide in exactly that kind of code.

## Architecture

New package `oddish/src/oddish/preflight/`:

```
preflight/
  registry.py      # CHECKS: list[Check]; each = id, description, fn
  models.py        # Finding, Severity, Check
  runner.py        # run_checks(paths) -> list[Finding]
  checks/
    dockerfile_references.py
    task_absolute_path.py
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
| Unsuppressed fetch in Dockerfile / solve.sh / test.sh | error |
| `.git` reachable in the image | error |
| `.git` absent but no `.dockerignore` excluding it | warn |
| `allow_internet = true` with no `open_internet_justification` | error |
| `COPY`/`ADD` source that does not exist | error |
| Host-absolute path in task files | error |
| `.patch` / `.diff` in `solution/` | error |
| Brittle anti-cheat regex (unsuppressed) | error |

`warn` exists so that "you have no `.dockerignore`" is not shouted with the same
volume as "your image contains the answer."

## The provenance check

Two rules.

### Fetch rule

Scan `environment/Dockerfile`, `solution/solve.sh`, and `tests/test.sh` for:

- `git clone`, `git fetch`
- `pip install git+…`
- archive URLs: `*/archive/*.tar.gz`, `codeload.github.com`, release tarballs

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

Error if:

- A `COPY` or `ADD` source directory contains a `.git`, or
- The task ships a `.git` and no `.dockerignore` excludes it.

Warn if there is no `.dockerignore` at all and no `.git` is currently present —
the leak is latent rather than live.

This is the quietest leak and the highest-value surface: the agent runs `git log`
in `/app` and reads the fix commit message, or `git diff HEAD~1` and reads the
answer outright.

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
