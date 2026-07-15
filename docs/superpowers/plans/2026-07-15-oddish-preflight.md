# Oddish Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate `oddish run` on a set of task-integrity checks before upload, so a task that leaks its own answer never costs a trial.

**Architecture:** A new `oddish.preflight` package holds pure check functions with the signature `(task_dir: Path, config: TaskConfig) -> list[Finding]`. They neither print nor exit. A registry lists them; a runner parses `task.toml` once per task and fans the checks over it. Rendering and exit codes live only at the CLI edge (`oddish/cli/preflight.py`), which is what lets the same check bodies serve `oddish preflight`, the auto-run inside `oddish run`, and eventually harbor-lh's CI via `--json`.

**Tech Stack:** Python 3.13, Typer, Rich, Harbor's `TaskConfig` (pydantic), pytest. Stdlib `re` and `pathlib` only inside checks — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-15-oddish-preflight-design.md`

## Global Constraints

- **Branch:** work on `preflight-spec` (already cut from `origin/main`). Never commit to `main`.
- **Package root:** the CLI package is at `oddish/src/oddish/` — note the nested `oddish/` repo subdir. Tests are at `oddish/tests/`. All `pytest` and `uv` commands run from `oddish/`.
- **No new dependencies.** Checks use stdlib `re` / `pathlib` and Harbor's already-vendored `TaskConfig`.
- **Checks are pure.** A check function never prints, never raises `typer.Exit`, never touches the network. It returns `list[Finding]`. Violating this breaks the CI-callable goal.
- **Parse `task.toml` once.** Checks receive an already-parsed `TaskConfig`; they must not re-read or re-parse it. Re-parsing drifts from Harbor's schema.
- **Error rendering convention:** `error_console.print("[red]…[/red]")` from `oddish.cli.config`, `[yellow]` for warnings, `[dim]` for asides, then `raise typer.Exit(1)`.
- **Severity:** `Severity.ERROR` blocks a run; `Severity.WARN` prints and does not block.
- **Suppression grammar:** `# provenance-ok: <reason>` with a reason of **≥10 characters**, mirroring harbor-lh's existing `# anti-cheat-ok:` idiom.
- **Every task ends with a commit.** Use conventional-commit prefixes (`feat:`, `test:`, `docs:`).

## Verified Harbor API facts

These were confirmed against `harbor/models/task/config.py`. Do not re-derive:

- `TaskConfig.metadata: dict[str, Any]` — free-form; `[metadata]` in `task.toml`.
- `TaskConfig.environment: EnvironmentConfig(BaselineNetworkPolicyConfig)` → `.network_mode: NetworkMode`, **default `NetworkMode.PUBLIC`**.
- `TaskConfig.agent: AgentConfig(PhaseNetworkPolicyConfig)` → `.network_mode: NetworkMode | None`, default `None` meaning *inherit the baseline*.
- `TaskConfig.verifier: VerifierConfig(PhaseNetworkPolicyConfig)` → same shape as agent.
- `NetworkMode` is a `str, Enum` with `NO_NETWORK = "no-network"`, `PUBLIC = "public"`, `ALLOWLIST = "allowlist"`.
- Parse with `TaskConfig.model_validate_toml(path.read_text())`.

## File structure

**Create:**
- `oddish/src/oddish/preflight/__init__.py` — public exports
- `oddish/src/oddish/preflight/models.py` — `Severity`, `Finding`, `Check`
- `oddish/src/oddish/preflight/runner.py` — `run_checks`, `has_errors`
- `oddish/src/oddish/preflight/registry.py` — `CHECKS`
- `oddish/src/oddish/preflight/checks/__init__.py`
- `oddish/src/oddish/preflight/checks/dockerfile_leaks.py`
- `oddish/src/oddish/preflight/checks/closed_internet.py`
- `oddish/src/oddish/preflight/checks/solution_format.py`
- `oddish/src/oddish/preflight/checks/anti_cheat_soundness.py`
- `oddish/src/oddish/preflight/checks/provenance.py`
- `oddish/src/oddish/cli/preflight.py` — `preflight` command, `render_findings`, `gate_preflight`
- `oddish/tests/test_preflight_runner.py`
- `oddish/tests/test_preflight_checks.py`
- `oddish/tests/test_cli_preflight.py`

**Modify:**
- `oddish/tests/conftest.py` — add the `make_task` fixture
- `oddish/src/oddish/cli/__init__.py:19,40` — import and register `preflight`
- `oddish/src/oddish/cli/run.py` — add `--force`; call the gate
- `CHANGELOG.md`

---

### Task 1: Models, runner, and the shared task fixture

**Files:**
- Create: `oddish/src/oddish/preflight/__init__.py`, `oddish/src/oddish/preflight/models.py`, `oddish/src/oddish/preflight/runner.py`, `oddish/src/oddish/preflight/registry.py`, `oddish/src/oddish/preflight/checks/__init__.py`
- Modify: `oddish/tests/conftest.py`
- Test: `oddish/tests/test_preflight_runner.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Severity.ERROR` / `Severity.WARN` (`StrEnum`)
  - `Finding(check_id: str, severity: Severity, task_dir: Path, message: str, path: Path | None = None, line: int | None = None, fix_hint: str | None = None)`, with `.to_dict() -> dict[str, object]`
  - `Check(id: str, description: str, fn: CheckFn)` where `CheckFn = Callable[[Path, TaskConfig], list[Finding]]`
  - `run_checks(task_dirs: list[Path], checks: list[Check] | None = None) -> list[Finding]`
  - `has_errors(findings: list[Finding]) -> bool`
  - `CHECKS: list[Check]` (empty in this task; later tasks append)
  - pytest fixture `make_task(name=..., task_toml=..., dockerfile=..., test_sh=..., solve_sh=..., extra_files=...) -> Path`

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_preflight_runner.py`:

```python
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Check, Finding, Severity
from oddish.preflight.runner import has_errors, run_checks


def _boom(task_dir: Path, config: TaskConfig) -> list[Finding]:
    return [
        Finding(
            check_id="boom",
            severity=Severity.ERROR,
            task_dir=task_dir,
            message="it exploded",
            path=task_dir / "task.toml",
            line=3,
            fix_hint="stop it",
        )
    ]


def _quiet(task_dir: Path, config: TaskConfig) -> list[Finding]:
    return []


BOOM = Check(id="boom", description="always fails", fn=_boom)
QUIET = Check(id="quiet", description="never fails", fn=_quiet)


def test_run_checks_collects_findings_from_each_check(make_task):
    task_dir = make_task()
    findings = run_checks([task_dir], checks=[BOOM, QUIET])
    assert len(findings) == 1
    assert findings[0].check_id == "boom"
    assert findings[0].task_dir == task_dir


def test_run_checks_fans_over_every_task_dir(make_task):
    a = make_task("task-a")
    b = make_task("task-b")
    findings = run_checks([a, b], checks=[BOOM])
    assert {f.task_dir for f in findings} == {a, b}


def test_run_checks_reports_unparseable_task_toml_and_skips_checks(make_task):
    task_dir = make_task(task_toml="this is not valid toml {{{")
    findings = run_checks([task_dir], checks=[BOOM])
    assert len(findings) == 1
    assert findings[0].check_id == "task_config"
    assert findings[0].severity is Severity.ERROR
    assert "Could not parse task.toml" in findings[0].message


def test_has_errors_is_false_for_warnings_only(make_task):
    task_dir = make_task()
    warn = Finding(
        check_id="w", severity=Severity.WARN, task_dir=task_dir, message="meh"
    )
    assert has_errors([warn]) is False
    assert has_errors([warn, _boom(task_dir, None)[0]]) is True


def test_finding_to_dict_is_json_safe(make_task):
    task_dir = make_task()
    payload = _boom(task_dir, None)[0].to_dict()
    assert payload["check_id"] == "boom"
    assert payload["severity"] == "error"
    assert payload["line"] == 3
    assert isinstance(payload["task_dir"], str)
    assert isinstance(payload["path"], str)
```

- [ ] **Step 2: Add the `make_task` fixture**

Append to `oddish/tests/conftest.py`. This promotes `_write_minimal_task` (currently private to `tests/test_cli_upload.py:15`) into a shared factory — preflight is its third caller. The default `task.toml` sets `network_mode = "no-network"` so the default task is clean and does not trip `closed_internet` in later tasks.

```python
from pathlib import Path
from typing import Callable

import pytest

_DEFAULT_TASK_TOML = """\
version = "1.0"

[metadata]
difficulty = "easy"
description = "a sample task"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
network_mode = "no-network"
"""


@pytest.fixture
def make_task(tmp_path: Path) -> Callable[..., Path]:
    """Build a minimal, valid Harbor task directory under tmp_path.

    Returns a factory so a single test can build several tasks. Every keyword
    overrides one file; ``None`` omits the file entirely.
    """

    def _make(
        name: str = "sample-task",
        *,
        task_toml: str | None = _DEFAULT_TASK_TOML,
        dockerfile: str | None = "FROM ubuntu:24.04\n",
        test_sh: str | None = "#!/bin/sh\nexit 0\n",
        solve_sh: str | None = None,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        task_dir = tmp_path / name
        task_dir.mkdir(parents=True, exist_ok=True)

        if task_toml is not None:
            (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")
        (task_dir / "instruction.md").write_text("Solve the task.\n", encoding="utf-8")

        if dockerfile is not None:
            (task_dir / "environment").mkdir(exist_ok=True)
            (task_dir / "environment" / "Dockerfile").write_text(
                dockerfile, encoding="utf-8"
            )
        if test_sh is not None:
            (task_dir / "tests").mkdir(exist_ok=True)
            (task_dir / "tests" / "test.sh").write_text(test_sh, encoding="utf-8")
        if solve_sh is not None:
            (task_dir / "solution").mkdir(exist_ok=True)
            (task_dir / "solution" / "solve.sh").write_text(solve_sh, encoding="utf-8")

        for rel, content in (extra_files or {}).items():
            dest = task_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        return task_dir

    return _make
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_preflight_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oddish.preflight'`

- [ ] **Step 4: Write `models.py`**

Create `oddish/src/oddish/preflight/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from harbor.models.task.config import TaskConfig


class Severity(StrEnum):
    """How much a finding matters.

    ERROR blocks a run; WARN is printed and ignored. The split exists so
    "you have no .dockerignore" is not shouted at the same volume as
    "your image contains the answer".
    """

    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True)
class Finding:
    check_id: str
    severity: Severity
    task_dir: Path
    message: str
    path: Path | None = None
    line: int | None = None
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "severity": self.severity.value,
            "task_dir": str(self.task_dir),
            "path": str(self.path) if self.path is not None else None,
            "line": self.line,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


# A check never prints, never exits, and never touches the network: it maps a
# task directory and its parsed config to findings. That purity is what lets
# the same bodies serve the CLI, `oddish run`, and external CI.
CheckFn = Callable[[Path, TaskConfig], list["Finding"]]


@dataclass(frozen=True)
class Check:
    id: str
    description: str
    fn: CheckFn
```

- [ ] **Step 5: Write `registry.py` and `checks/__init__.py`**

Create `oddish/src/oddish/preflight/checks/__init__.py` as an empty file.

Create `oddish/src/oddish/preflight/registry.py`:

```python
from __future__ import annotations

from oddish.preflight.models import Check

# Populated as each check lands. Order is display order.
CHECKS: list[Check] = []
```

- [ ] **Step 6: Write `runner.py`**

Create `oddish/src/oddish/preflight/runner.py`:

```python
from __future__ import annotations

from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Check, Finding, Severity
from oddish.preflight.registry import CHECKS


def run_checks(
    task_dirs: list[Path], checks: list[Check] | None = None
) -> list[Finding]:
    """Run every check against every task directory.

    task.toml is parsed once per task and handed to each check. A task whose
    config will not parse yields a single finding and is skipped: every check
    needs the config, so running them would just produce noise on top of the
    real problem.
    """
    active = CHECKS if checks is None else checks
    findings: list[Finding] = []

    for task_dir in task_dirs:
        config_path = task_dir / "task.toml"
        try:
            config = TaskConfig.model_validate_toml(config_path.read_text())
        except Exception as e:
            findings.append(
                Finding(
                    check_id="task_config",
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=config_path,
                    message=f"Could not parse task.toml: {type(e).__name__}: {e}",
                    fix_hint="Fix task.toml so Harbor can load the task.",
                )
            )
            continue

        for check in active:
            findings.extend(check.fn(task_dir, config))

    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity is Severity.ERROR for f in findings)
```

- [ ] **Step 7: Write `preflight/__init__.py`**

```python
from __future__ import annotations

from oddish.preflight.models import Check, CheckFn, Finding, Severity
from oddish.preflight.registry import CHECKS
from oddish.preflight.runner import has_errors, run_checks

__all__ = [
    "CHECKS",
    "Check",
    "CheckFn",
    "Finding",
    "Severity",
    "has_errors",
    "run_checks",
]
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_preflight_runner.py -v`
Expected: 5 passed

- [ ] **Step 9: Verify the existing suite still passes**

Run: `cd oddish && uv run pytest tests/test_cli_upload.py -v`
Expected: PASS — the conftest addition must not disturb the existing helper.

- [ ] **Step 10: Commit**

```bash
git add oddish/src/oddish/preflight oddish/tests/test_preflight_runner.py oddish/tests/conftest.py
git commit -m "feat(preflight): add check models, registry, and runner"
```

---

### Task 2: REMOVED — `dockerfile_leaks`

**Do not implement.** This task was implemented, reviewed three times, and then
reverted (commit `8d14eece`). It is retained here as a numbered placeholder so
Tasks 3-8 keep their numbers and briefs.

**Why:** Harbor builds the task image with the build context set to
`environment/` (`harbor/environments/docker/docker.py:240`), and `tests/` and
`solution/` are *siblings* of `environment/`, not children. A task Dockerfile
therefore cannot `COPY` the real grader or the real solution — those paths lie
outside the build context and Docker refuses them. The check guarded a door
Docker welds shut, while its broadened form produced ERROR-severity false
positives on ordinary idioms (`FROM node:20 AS tests`, `RUN echo "all tests
pass"`).

The real leak in this family — an author *duplicating* the grader inside
`environment/` — needs a content comparison, not a Dockerfile grep. It is a
candidate follow-up, not a v1 check.

**What survives:** Task 2 created `oddish/tests/test_preflight_checks.py` with
the shared `_config(task_dir)` helper. The revert kept that scaffold; Task 3
appends to it as planned.

**The generalizable rule, which Task 6 depends on:** only what is inside
`environment/` can enter the image.

---

### Task 3: `closed_internet` check

**Files:**
- Create: `oddish/src/oddish/preflight/checks/closed_internet.py`
- Modify: `oddish/src/oddish/preflight/registry.py`
- Test: `oddish/tests/test_preflight_checks.py` (append)

**Interfaces:**
- Consumes: `Finding`, `Severity`; `NetworkMode`, `TaskConfig` from `harbor.models.task.config`.
- Produces: `closed_internet.CHECK_ID = "closed_internet"`, `closed_internet.check(task_dir, config) -> list[Finding]`.

**Semantics (verified against Harbor):** a phase is open when its effective `network_mode` is `PUBLIC`. The environment baseline defaults to `PUBLIC`; `[agent]` and `[verifier]` default to `None`, meaning *inherit the baseline*. So a `task.toml` with no network config at all is fully open — that implicit case is the main one this check exists to catch.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/test_preflight_checks.py`:

```python
from oddish.preflight.checks import closed_internet

_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "open task"

[environment]
network_mode = "public"
"""

_IMPLICIT_TOML = """\
version = "1.0"

[metadata]
description = "no network config at all"
"""

_CLOSED_TOML = """\
version = "1.0"

[metadata]
description = "closed task"

[environment]
network_mode = "no-network"
"""

_JUSTIFIED_TOML = """\
version = "1.0"

[metadata]
description = "open but justified"
open_internet_justification = "Needs live PyPI to resolve the dependency graph under test."

[environment]
network_mode = "public"
"""


def test_closed_internet_flags_explicit_public(make_task):
    task_dir = make_task(task_toml=_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "environment baseline" in findings[0].message


def test_closed_internet_flags_implicit_public_default(make_task):
    # Harbor defaults network_mode to public, so a task.toml that says nothing
    # about the network is fully open. This is the case that matters most.
    task_dir = make_task(task_toml=_IMPLICIT_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_passes_no_network(make_task):
    task_dir = make_task(task_toml=_CLOSED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


def test_closed_internet_passes_with_a_real_justification(make_task):
    task_dir = make_task(task_toml=_JUSTIFIED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


@pytest.mark.parametrize("placeholder", ["TBD", "todo", "n/a", "xxx", "  ", "short"])
def test_closed_internet_rejects_placeholder_justifications(make_task, placeholder):
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        f'description = "open task"\nopen_internet_justification = "{placeholder}"',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_flags_an_open_agent_over_a_closed_baseline(make_task):
    toml = _CLOSED_TOML + '\n[agent]\nnetwork_mode = "public"\n'
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert "[agent]" in findings[0].message


def test_closed_internet_passes_allowlist(make_task):
    toml = """\
version = "1.0"

[metadata]
description = "allowlisted"

[environment]
network_mode = "allowlist"
allowed_hosts = ["pypi.org"]
"""
    task_dir = make_task(task_toml=toml)
    assert closed_internet.check(task_dir, _config(task_dir)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -k closed_internet -v`
Expected: FAIL with `ImportError: cannot import name 'closed_internet'`

- [ ] **Step 3: Write the implementation**

Create `oddish/src/oddish/preflight/checks/closed_internet.py`:

```python
from __future__ import annotations

from pathlib import Path

from harbor.models.task.config import NetworkMode, TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "closed_internet"

_JUSTIFICATION_KEY = "open_internet_justification"
_MIN_JUSTIFICATION_CHARS = 20

# Lowercased exact matches that are not justifications.
_PLACEHOLDERS = frozenset(
    {"tbd", "todo", "n/a", "na", "none", "placeholder", "xxx", "fixme", "-"}
)


def _is_real_justification(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < _MIN_JUSTIFICATION_CHARS:
        return False
    return stripped.lower() not in _PLACEHOLDERS


def _effective(
    phase_mode: NetworkMode | None, baseline: NetworkMode
) -> NetworkMode:
    """A phase with no explicit mode inherits the environment baseline."""
    return baseline if phase_mode is None else phase_mode


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    baseline = config.environment.network_mode
    phases = {
        "environment baseline": baseline,
        "[agent]": _effective(config.agent.network_mode, baseline),
        "[verifier]": _effective(config.verifier.network_mode, baseline),
    }

    open_phases = [
        name for name, mode in phases.items() if mode is NetworkMode.PUBLIC
    ]
    if not open_phases:
        return []

    justification = str(config.metadata.get(_JUSTIFICATION_KEY) or "")
    if _is_real_justification(justification):
        return []

    where = ", ".join(open_phases)
    return [
        Finding(
            check_id=CHECK_ID,
            severity=Severity.ERROR,
            task_dir=task_dir,
            path=task_dir / "task.toml",
            message=(
                f"Open internet ({where}) with no justification. An agent with "
                "network access can fetch the upstream repo regardless of what "
                "the image ships."
            ),
            fix_hint=(
                'Set network_mode = "no-network" (or "allowlist" with '
                "allowed_hosts), or add a [metadata] "
                f"{_JUSTIFICATION_KEY} of at least "
                f"{_MIN_JUSTIFICATION_CHARS} characters explaining why."
            ),
        )
    ]
```

- [ ] **Step 4: Register the check**

In `oddish/src/oddish/preflight/registry.py`, add the import and append to `CHECKS`:

```python
from oddish.preflight.checks import closed_internet, dockerfile_leaks
```

```python
    Check(
        id=closed_internet.CHECK_ID,
        description="Open internet requires a justification",
        fn=closed_internet.check,
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -v`
Expected: all passed (8 from Task 2 + 12 new)

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/preflight oddish/tests/test_preflight_checks.py
git commit -m "feat(preflight): require a justification for open-internet tasks"
```

---

### Task 4: `solution_format` check

**Files:**
- Create: `oddish/src/oddish/preflight/checks/solution_format.py`
- Modify: `oddish/src/oddish/preflight/registry.py`
- Test: `oddish/tests/test_preflight_checks.py` (append)

**Interfaces:**
- Consumes: `Finding`, `Severity`.
- Produces: `solution_format.CHECK_ID = "solution_format"`, `solution_format.check(task_dir, config) -> list[Finding]`.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/test_preflight_checks.py`:

```python
from oddish.preflight.checks import solution_format


def test_solution_format_flags_patch_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp fix.py /app/\n",
        extra_files={"solution/fix.patch": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "fix.patch" in findings[0].message


def test_solution_format_flags_diff_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\n",
        extra_files={"solution/fix.diff": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1


@pytest.mark.parametrize(
    "cmd",
    ["git apply fix.patch", "patch -p1 < fix.patch", "git am fix.patch"],
)
def test_solution_format_flags_patch_application_in_solve_sh(make_task, cmd):
    task_dir = make_task(solve_sh=f"#!/bin/sh\n{cmd}\n")
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].line == 2


def test_solution_format_passes_a_readable_solution(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp /solution/fix.py /app/fix.py\n",
        extra_files={"solution/fix.py": "print('fixed')\n"},
    )
    assert solution_format.check(task_dir, _config(task_dir)) == []


def test_solution_format_is_silent_without_a_solution_dir(make_task):
    task_dir = make_task()
    assert solution_format.check(task_dir, _config(task_dir)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -k solution_format -v`
Expected: FAIL with `ImportError: cannot import name 'solution_format'`

- [ ] **Step 3: Write the implementation**

Create `oddish/src/oddish/preflight/checks/solution_format.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "solution_format"

_PATCH_SUFFIXES = frozenset({".patch", ".diff"})
_PATCH_APPLY_RE = re.compile(r"\b(?:git\s+apply|git\s+am|patch\s+-p\d)\b")


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    solution_dir = task_dir / "solution"
    if not solution_dir.is_dir():
        return []

    findings: list[Finding] = []

    for path in sorted(solution_dir.rglob("*")):
        if path.is_file() and path.suffix in _PATCH_SUFFIXES:
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    message=(
                        f"solution/ contains a patch file ({path.name}). "
                        "Solutions must be readable source, not diffs."
                    ),
                    fix_hint=(
                        "Ship the resulting source files and have solve.sh copy "
                        "or compile them."
                    ),
                )
            )

    solve_sh = solution_dir / "solve.sh"
    if solve_sh.is_file():
        text = solve_sh.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _PATCH_APPLY_RE.search(line):
                findings.append(
                    Finding(
                        check_id=CHECK_ID,
                        severity=Severity.ERROR,
                        task_dir=task_dir,
                        path=solve_sh,
                        line=lineno,
                        message="solve.sh applies a patch instead of shipping source.",
                        fix_hint=(
                            "Replace the patch application with the post-fix "
                            "source files."
                        ),
                    )
                )

    return findings
```

- [ ] **Step 4: Register the check**

In `registry.py`, import `solution_format` and append:

```python
    Check(
        id=solution_format.CHECK_ID,
        description="Solutions are readable source, not patches",
        fn=solution_format.check,
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/preflight oddish/tests/test_preflight_checks.py
git commit -m "feat(preflight): require readable solutions, not patch files"
```

---

### Task 5: `anti_cheat_soundness` check

A near-verbatim port of harbor-lh's `ci_checks/_anti_cheat_scan.py`. The regexes below are copied from that file unchanged — do not "improve" them; their exact tuning is what keeps the false-positive rate survivable.

**Files:**
- Create: `oddish/src/oddish/preflight/checks/anti_cheat_soundness.py`
- Modify: `oddish/src/oddish/preflight/registry.py`
- Test: `oddish/tests/test_preflight_checks.py` (append)

**Interfaces:**
- Consumes: `Finding`, `Severity`.
- Produces: `anti_cheat_soundness.CHECK_ID = "anti_cheat_soundness"`, `anti_cheat_soundness.check(task_dir, config) -> list[Finding]`.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/test_preflight_checks.py`:

```python
from oddish.preflight.checks import anti_cheat_soundness


def test_anti_cheat_flags_import_scan_regex(make_task):
    task_dir = make_task(
        extra_files={"tests/test_cheat.py": 'import re\nBAD = re.compile(r"\\bimport\\s+stripe\\b")\n'}
    )
    findings = anti_cheat_soundness.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 2


def test_anti_cheat_flags_bare_word_library_regex(make_task):
    task_dir = make_task(
        extra_files={"tests/test_cheat.py": 'import re\nBAD = re.compile(r"\\bopenpyxl\\b")\n'}
    )
    findings = anti_cheat_soundness.check(task_dir, _config(task_dir))
    assert len(findings) == 1


def test_anti_cheat_allows_hostname_regex(make_task):
    # Slashes and dots mean this is a URL, not a library-identifier scan.
    task_dir = make_task(
        extra_files={"tests/test_ok.py": 'import re\nOK = re.compile(r"https?://api\\.stripe\\.com")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_allows_scoring_tokens(make_task):
    task_dir = make_task(
        extra_files={"tests/test_ok.py": 'import re\nOK = re.compile(r"\\bpassed\\b")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_respects_suppression_comment(make_task):
    task_dir = make_task(
        extra_files={
            "tests/test_ok.py": (
                'import re\n'
                'OK = re.compile(r"\\bopenpyxl\\b")  # anti-cheat-ok: spec forbids this exact library by name\n'
            )
        }
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_ignores_non_test_files(make_task):
    task_dir = make_task(
        extra_files={"environment/app.py": 'import re\nX = re.compile(r"\\bopenpyxl\\b")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -k anti_cheat -v`
Expected: FAIL with `ImportError: cannot import name 'anti_cheat_soundness'`

- [ ] **Step 3: Write the implementation**

Create `oddish/src/oddish/preflight/checks/anti_cheat_soundness.py`:

```python
from __future__ import annotations

import os
import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "anti_cheat_soundness"

# Ported verbatim from harbor-lh ci_checks/_anti_cheat_scan.py. The tuning of
# these three patterns (and B3's deny/allow lists in particular) is what keeps
# the false-positive rate tolerable — do not adjust without re-testing against
# real task test suites.

# B1 — regex literals matching `import <word>` or `from <word>`.
B1_RE = re.compile(r"""r['"][^'"]*\\b?(?:import|from)\\s\+[^'"]*['"]""")

# B2 — shell `grep` for import/from, but only over an agent-writable tree.
B2_RE = re.compile(
    r"""grep\s+(?:-[a-zA-Z]+\s+)*['"][^'"]*(?:\\b)?(?:import|from)(?:\\s|\s)[^'"]*['"]"""
)

# B3 — bare-word regex `\b<word>\b` for a single lowercase identifier: a
# library-name scan. 5-char minimum avoids colliding with match groups.
B3_RE = re.compile(r"""r['"](\\b)?([a-z][a-z0-9_-]{4,})(\\b)?['"]""")
B3_DENY_CHARS = set("/.\\:?*+|()[]{}<>=!&;,$%@#^~\"' \t`")
B3_ALLOW_TOKENS = frozenset(
    {
        "passed", "failed", "total", "score", "reward",
        "metrics", "logs", "tests", "stdout", "stderr",
        "pytest", "result", "results", "report", "output",
        "ctrf", "verifier", "artifacts", "branch", "commit",
    }
)

SUPPRESS_RE = re.compile(r"#\s*anti-cheat-ok\s*:\s*\S[^\n]{9,}")

_B2_AGENT_TREES = ("/app", "rglob", "/workspace")

_FIX_HINT = (
    "Prefer a capability-level defense: encrypt golden assets, remove the "
    "library at verify time, run python3 -I -S, or block egress in task.toml. "
    "If the regex really is right, suppress with `# anti-cheat-ok: <reason>`."
)


def _is_test_file(path: Path) -> bool:
    if path.name == "test.sh":
        return True
    if path.suffix in {".py", ".sh"}:
        return f"{os.sep}tests{os.sep}" in str(path)
    return False


def _scan_line(line: str) -> str | None:
    """Return a description of the brittle pattern on this line, or None."""
    if SUPPRESS_RE.search(line):
        return None
    if B1_RE.search(line):
        return "regex scans agent source for `import <lib>`"
    if B2_RE.search(line) and any(n in line for n in _B2_AGENT_TREES):
        return "grep scans agent source for import/from"
    for m in B3_RE.finditer(line):
        literal = m.group(0)
        inner = literal[2:-1]
        if inner.startswith("\\b"):
            inner = inner[2:]
        if inner.endswith("\\b"):
            inner = inner[:-2]
        if not inner:
            continue
        if any(c in B3_DENY_CHARS for c in inner):
            continue
        if inner.lower() in B3_ALLOW_TOKENS:
            continue
        return f"bare-word regex r'{inner}' scans for a library name"
    return None


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    tests_dir = task_dir / "tests"
    if not tests_dir.is_dir():
        return []

    findings: list[Finding] = []
    for path in sorted(tests_dir.rglob("*")):
        if not path.is_file() or not _is_test_file(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            kind = _scan_line(line)
            if kind is None:
                continue
            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    line=lineno,
                    message=(
                        f"Brittle anti-cheat: {kind}. Source scans false-positive "
                        "on legitimate mentions and are trivially evaded."
                    ),
                    fix_hint=_FIX_HINT,
                )
            )
    return findings
```

- [ ] **Step 4: Register the check**

In `registry.py`, import `anti_cheat_soundness` and append:

```python
    Check(
        id=anti_cheat_soundness.CHECK_ID,
        description="No brittle source-scanning anti-cheat",
        fn=anti_cheat_soundness.check,
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/preflight oddish/tests/test_preflight_checks.py
git commit -m "feat(preflight): port harbor-lh anti-cheat soundness scan"
```

---

### Task 6: `provenance` check — the net-new one

Two rules: repo fetches, and `.git` in the image.

**Files:**
- Create: `oddish/src/oddish/preflight/checks/provenance.py`
- Modify: `oddish/src/oddish/preflight/registry.py`
- Test: `oddish/tests/test_preflight_checks.py` (append)

**Interfaces:**
- Consumes: `Finding`, `Severity`.
- Produces: `provenance.CHECK_ID = "provenance"`, `provenance.check(task_dir, config) -> list[Finding]`.

**The `.git` rule is scoped to `environment/`.** Harbor builds the task image with the build context set to `environment/` (`harbor/environments/docker/docker.py:240`). Only what is inside the build context can enter the image, so only a `.git` under `environment/` can reach the agent — Docker refuses paths outside the context.

So: **a `.git` anywhere under `environment/` is an error unless `.dockerignore` excludes it.** A `.git` elsewhere in the task dir is ignored; flagging it would be an ERROR-severity false positive on something that provably cannot happen. (That mistake is what got Task 2 removed — see its removal record above.)

This also means the enclosing repo's own `.git` is a non-issue: it lives at the repo root, far outside `environment/`.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/test_preflight_checks.py`:

```python
from oddish.preflight.checks import provenance


@pytest.mark.parametrize(
    "line",
    [
        "RUN git clone https://github.com/foo/bar /src",
        "RUN git fetch origin main",
        "RUN pip install git+https://github.com/foo/bar@abc123",
        "RUN curl -L https://github.com/foo/bar/archive/main.tar.gz | tar xz",
        "RUN wget https://codeload.github.com/foo/bar/tar.gz/main",
    ],
)
def test_provenance_flags_repo_fetches_in_dockerfile(make_task, line):
    task_dir = make_task(dockerfile=f"FROM ubuntu:24.04\n{line}\n")
    findings = [
        f for f in provenance.check(task_dir, _config(task_dir))
        if "fetch" in f.message or "clone" in f.message.lower()
    ]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 2


def test_provenance_flags_fetch_in_solve_sh(make_task):
    task_dir = make_task(solve_sh="#!/bin/sh\ngit clone https://github.com/foo/bar\n")
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if f.line == 2]
    assert len(findings) == 1


def test_provenance_flags_fetch_in_test_sh(make_task):
    task_dir = make_task(test_sh="#!/bin/sh\ngit fetch origin\n")
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if f.line == 2]
    assert len(findings) == 1


def test_provenance_accepts_a_valid_suppression(make_task):
    task_dir = make_task(
        dockerfile=(
            "FROM ubuntu:24.04\n"
            "RUN git clone https://github.com/foo/dep  # provenance-ok: pinned third-party dep, not the task upstream\n"
        ),
        extra_files={".dockerignore": ".git\n"},
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_rejects_a_too_short_suppression_reason(make_task):
    task_dir = make_task(
        dockerfile=(
            "FROM ubuntu:24.04\n"
            "RUN git clone https://github.com/foo/dep  # provenance-ok: dep\n"
        ),
        extra_files={".dockerignore": ".git\n"},
    )
    findings = provenance.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert "at least 10 characters" in findings[0].fix_hint


def test_provenance_rejects_a_suppression_with_no_reason(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\nRUN git clone https://x/y  # provenance-ok:\n",
        extra_files={".dockerignore": ".git\n"},
    )
    assert len(provenance.check(task_dir, _config(task_dir))) == 1


def test_provenance_ignores_comments(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\n# RUN git clone https://github.com/foo/bar\n",
        extra_files={".dockerignore": ".git\n"},
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_flags_a_git_dir_inside_the_build_context(make_task):
    task_dir = make_task(extra_files={"environment/repo/.git/HEAD": "ref: refs/heads/main\n"})
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if ".git" in f.message]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_provenance_accepts_a_git_dir_excluded_by_dockerignore(make_task):
    task_dir = make_task(
        extra_files={
            "environment/repo/.git/HEAD": "ref: refs/heads/main\n",
            ".dockerignore": "**/.git\n",
        }
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


@pytest.mark.parametrize(
    "git_file",
    ["solution/repo/.git/HEAD", "tests/repo/.git/HEAD", ".git/HEAD"],
)
def test_provenance_ignores_a_git_dir_outside_the_build_context(make_task, git_file):
    # Only environment/ is the Docker build context; Docker refuses paths
    # outside it, so a .git here provably cannot reach the agent's image.
    # Flagging it would be an ERROR on something that cannot happen.
    task_dir = make_task(
        extra_files={git_file: "ref: refs/heads/main\n", ".dockerignore": "**/.git\n"}
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_warns_when_no_dockerignore_exists(make_task):
    task_dir = make_task()
    findings = provenance.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN


def test_provenance_is_clean_with_a_dockerignore_and_no_git(make_task):
    task_dir = make_task(extra_files={".dockerignore": ".git/\n"})
    assert provenance.check(task_dir, _config(task_dir)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -k provenance -v`
Expected: FAIL with `ImportError: cannot import name 'provenance'`

- [ ] **Step 3: Write the implementation**

Create `oddish/src/oddish/preflight/checks/provenance.py`:

```python
from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "provenance"

_MIN_REASON_CHARS = 10

# A regex cannot tell the task's own upstream repo from an unrelated pinned
# dependency, and getting that wrong in the permissive direction hands the agent
# the fix commit. So every fetch is flagged and the author annotates the
# legitimate ones. The grammar mirrors harbor-lh's `# anti-cheat-ok:` so authors
# learn one suppression form, not two.
_SUPPRESS_RE = re.compile(r"#\s*provenance-ok\s*:\s*(\S.*)$")

_FETCH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+clone\b"), "git clone"),
    (re.compile(r"\bgit\s+fetch\b"), "git fetch"),
    (re.compile(r"\bpip\s+install\b[^\n]*\bgit\+"), "pip install git+"),
    (
        re.compile(r"https?://[^\s\"']*/archive/[^\s\"']*\.(?:tar\.gz|tgz|zip)"),
        "repo archive URL",
    ),
    (re.compile(r"https?://codeload\.[^\s\"']+"), "codeload URL"),
)

# Relative to the task dir. These are the files that build or drive the image.
_SCAN_TARGETS = (
    Path("environment") / "Dockerfile",
    Path("solution") / "solve.sh",
    Path("tests") / "test.sh",
)

_DOCKERIGNORE_GIT_ENTRIES = frozenset({".git", ".git/", "**/.git", "**/.git/", "*/.git"})


def _suppression_reason(line: str) -> str | None:
    """The reason text on this line's `# provenance-ok:` comment, if any."""
    m = _SUPPRESS_RE.search(line)
    return m.group(1).strip() if m else None


def _fetch_findings(task_dir: Path) -> list[Finding]:
    findings: list[Finding] = []

    for rel in _SCAN_TARGETS:
        path = task_dir / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            hit = next((label for p, label in _FETCH_PATTERNS if p.search(line)), None)
            if hit is None:
                continue

            reason = _suppression_reason(line)
            if reason is not None and len(reason) >= _MIN_REASON_CHARS:
                continue

            findings.append(
                Finding(
                    check_id=CHECK_ID,
                    severity=Severity.ERROR,
                    task_dir=task_dir,
                    path=path,
                    line=lineno,
                    message=(
                        f"{hit} fetches a repository. If this is the task's own "
                        "upstream, its history hands the agent the fix commit."
                    ),
                    fix_hint=(
                        "Vendor the source at a pinned revision with no history, "
                        "or annotate the line with `# provenance-ok: <reason>` "
                        f"(reason must be at least {_MIN_REASON_CHARS} characters)."
                    ),
                )
            )

    return findings


def _dockerignore_excludes_git(task_dir: Path) -> bool:
    dockerignore = task_dir / ".dockerignore"
    if not dockerignore.is_file():
        return False
    entries = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    }
    return bool(entries & _DOCKERIGNORE_GIT_ENTRIES)


def _git_findings(task_dir: Path) -> list[Finding]:
    # Scoped to the build context. Harbor builds the image with context set to
    # environment/ (harbor/environments/docker/docker.py:240), and Docker
    # refuses paths outside it — so only a .git under environment/ can reach the
    # agent. A .git elsewhere in the task dir provably cannot be baked in;
    # flagging it would be an ERROR on something that cannot happen.
    env_dir = task_dir / "environment"
    if not env_dir.is_dir():
        return []

    git_paths = sorted(p for p in env_dir.rglob(".git"))
    excluded = _dockerignore_excludes_git(task_dir)

    if git_paths:
        if excluded:
            return []
        return [
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=p,
                message=(
                    "environment/ ships a .git directory, so it lands in the "
                    "agent's image. The agent can run `git log` and read the "
                    "fix commit straight out of it."
                ),
                fix_hint=(
                    "Delete the .git directory, or exclude it with a "
                    "`.dockerignore` containing `**/.git`."
                ),
            )
            for p in git_paths
        ]

    if not excluded:
        return [
            Finding(
                check_id=CHECK_ID,
                severity=Severity.WARN,
                task_dir=task_dir,
                message=(
                    "No .dockerignore excluding .git. Nothing leaks today, but "
                    "a future COPY of a git checkout into environment/ would go "
                    "unnoticed."
                ),
                fix_hint="Add a .dockerignore containing `**/.git`.",
            )
        ]

    return []


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    return _fetch_findings(task_dir) + _git_findings(task_dir)
```

- [ ] **Step 4: Register the check**

In `registry.py`, import `provenance` and insert it **second**, right after `dockerfile_leaks` — both are leak checks and should render together:

```python
    Check(
        id=provenance.CHECK_ID,
        description="No repo fetches or .git in the agent image",
        fn=provenance.check,
    ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_preflight_checks.py -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/preflight oddish/tests/test_preflight_checks.py
git commit -m "feat(preflight): add provenance check for repo fetches and .git leaks"
```

---

### Task 7: `oddish preflight` subcommand

**Files:**
- Create: `oddish/src/oddish/cli/preflight.py`
- Modify: `oddish/src/oddish/cli/__init__.py`
- Test: `oddish/tests/test_cli_preflight.py`

**Interfaces:**
- Consumes: `run_checks`, `has_errors`, `Finding`, `Severity`; `resolve_local_task_paths` from `oddish.cli.api`; `error_console`, `print_json` from `oddish.cli.config`.
- Produces:
  - `render_findings(findings: list[Finding], *, downgrade: bool = False) -> None`
  - `gate_preflight(findings: list[Finding], *, force: bool, json_output: bool = False) -> None` — used by Task 8
  - `preflight(...)` Typer command

- [ ] **Step 1: Write the failing test**

Create `oddish/tests/test_cli_preflight.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import app

runner = CliRunner()

_LEAKY_DOCKERFILE = "FROM ubuntu:24.04\nCOPY solution/solve.sh /app/\n"


def test_preflight_exits_zero_on_a_clean_task(make_task):
    task_dir = make_task(extra_files={".dockerignore": "**/.git\n"})
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 0


def test_preflight_exits_one_on_an_error_finding(make_task):
    task_dir = make_task(
        dockerfile=_LEAKY_DOCKERFILE, extra_files={".dockerignore": "**/.git\n"}
    )
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 1
    assert "dockerfile_leaks" in result.output


def test_preflight_exits_zero_when_only_warnings(make_task):
    # No .dockerignore -> a WARN from provenance, nothing more.
    task_dir = make_task()
    result = runner.invoke(app, ["preflight", str(task_dir)])
    assert result.exit_code == 0


def test_preflight_json_emits_a_parseable_document(make_task):
    task_dir = make_task(
        dockerfile=_LEAKY_DOCKERFILE, extra_files={".dockerignore": "**/.git\n"}
    )
    result = runner.invoke(app, ["preflight", str(task_dir), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["findings"][0]["check_id"] == "dockerfile_leaks"
    assert payload["findings"][0]["severity"] == "error"


def test_preflight_json_ok_true_on_clean_task(make_task):
    task_dir = make_task(extra_files={".dockerignore": "**/.git\n"})
    result = runner.invoke(app, ["preflight", str(task_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["findings"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_cli_preflight.py -v`
Expected: FAIL — exit code 2, "No such command 'preflight'"

- [ ] **Step 3: Write the implementation**

Create `oddish/src/oddish/cli/preflight.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from oddish.cli.api import resolve_local_task_paths
from oddish.cli.config import console, error_console, print_json
from oddish.preflight.models import Finding, Severity
from oddish.preflight.runner import has_errors, run_checks


def _location(finding: Finding) -> str:
    if finding.path is None:
        return ""
    loc = str(finding.path)
    if finding.line is not None:
        loc = f"{loc}:{finding.line}"
    return f" [dim]{loc}[/dim]"


def render_findings(findings: list[Finding], *, downgrade: bool = False) -> None:
    """Print findings grouped by task, errors first.

    ``downgrade`` renders errors in warning colours — used by ``--force``, where
    the findings are informational because the run proceeds regardless.
    """
    by_task: dict[Path, list[Finding]] = {}
    for f in findings:
        by_task.setdefault(f.task_dir, []).append(f)

    for task_dir, items in by_task.items():
        error_console.print(f"\n[bold]{task_dir}[/bold]")
        ordered = sorted(items, key=lambda f: 0 if f.severity is Severity.ERROR else 1)
        for f in ordered:
            is_error = f.severity is Severity.ERROR and not downgrade
            colour = "red" if is_error else "yellow"
            label = f.severity.value if not downgrade else "forced"
            error_console.print(
                f"  [{colour}]{label}[/{colour}] "
                f"[dim]{f.check_id}[/dim] {f.message}{_location(f)}"
            )
            if f.fix_hint:
                error_console.print(f"        [dim]{f.fix_hint}[/dim]")


def gate_preflight(
    findings: list[Finding], *, force: bool, json_output: bool = False
) -> None:
    """Render findings and abort unless clean or forced.

    Shared by ``oddish preflight`` and ``oddish run``. Under ``--force`` the
    findings are still printed: skipping the gate must not mean skipping the
    information about what is being skipped.
    """
    failed = has_errors(findings)

    if json_output:
        print_json({"ok": not failed, "findings": [f.to_dict() for f in findings]})
    elif findings:
        render_findings(findings, downgrade=force and failed)

    if not failed:
        return

    if force:
        error_console.print(
            "\n[yellow]Preflight failed but --force was given; submitting anyway.[/yellow]"
        )
        return

    error_console.print(
        "\n[red]Preflight failed.[/red] Fix the errors above, or re-run with "
        "[bold]--force[/bold] to submit anyway."
    )
    raise typer.Exit(1)


def preflight(
    path: Annotated[
        Optional[Path],
        typer.Argument(help="Path to task or dataset directory"),
    ] = None,
    path_option: Annotated[
        Optional[Path],
        typer.Option("--path", help="Path to task or dataset directory"),
    ] = None,
    dataset: Annotated[
        Optional[str],
        typer.Option("--dataset", help="Harbor dataset name to resolve tasks from"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit findings as JSON for CI consumers."),
    ] = False,
) -> None:
    """Check local tasks for integrity problems before spending trials on them."""
    task_paths = resolve_local_task_paths(
        path=path,
        path_option=path_option,
        dataset=dataset,
        task_names=None,
        exclude_task_names=None,
        n_tasks=None,
        quiet=json_output,
    )

    findings = run_checks(task_paths)
    gate_preflight(findings, force=False, json_output=json_output)

    if not json_output:
        console.print(
            f"\n[green]Preflight passed[/green] "
            f"[dim]({len(task_paths)} task(s))[/dim]"
        )
```

- [ ] **Step 4: Register the command**

In `oddish/src/oddish/cli/__init__.py`, add the import alongside the others (alphabetical, after `from oddish.cli.pull import pull`):

```python
from oddish.cli.preflight import preflight
```

And register it after `app.command()(upload)`:

```python
app.command()(preflight)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_cli_preflight.py -v`
Expected: 5 passed

- [ ] **Step 6: Verify the command is wired**

Run: `cd oddish && uv run oddish preflight --help`
Expected: help text containing "Check local tasks for integrity problems"

- [ ] **Step 7: Commit**

```bash
git add oddish/src/oddish/cli/preflight.py oddish/src/oddish/cli/__init__.py oddish/tests/test_cli_preflight.py
git commit -m "feat(cli): add oddish preflight subcommand with --json"
```

---

### Task 8: Wire preflight into `oddish run` behind `--force`

**Files:**
- Modify: `oddish/src/oddish/cli/run.py`, `CHANGELOG.md`
- Test: `oddish/tests/test_cli_preflight.py` (append)

**Interfaces:**
- Consumes: `gate_preflight` from `oddish.cli.preflight`; `run_checks` from `oddish.preflight.runner`.
- Produces: `--force` on `oddish run`.

**Naming note.** `run()` already has `--force-new-version` (`run.py:497`), which is about task versioning and unrelated. The bare `--force` is kept because it is the repo's idiom for "I know, do it anyway" (`cli/cancel.py:54`, `cli/backfill_analysis.py:42`) and it is what was asked for; the help text must therefore say precisely what it skips.

- [ ] **Step 1: Write the failing test**

Append to `oddish/tests/test_cli_preflight.py`.

**Patching gotcha — do not use string targets here.** `oddish.cli` re-exports the
`run` *command*, which shadows the `oddish.cli.run` *module*, so
`monkeypatch.setattr("oddish.cli.run.upload_tasks_with_progress", …)` resolves
`oddish.cli.run` to the function and fails. Pull the real module out with
`importlib`, exactly as `tests/test_cli_run_tpu.py:24` does.

```python
import importlib

run_module = importlib.import_module("oddish.cli.run")


def _stub_run_preamble(monkeypatch):
    """Neutralize the API-key/URL preamble that runs before the preflight gate."""
    monkeypatch.setattr(run_module, "get_api_url", lambda *a, **k: "http://x")
    monkeypatch.setattr(run_module, "require_api_key", lambda *a, **k: "key")


def test_run_aborts_before_upload_when_preflight_fails(make_task, monkeypatch):
    task_dir = make_task(
        dockerfile=_LEAKY_DOCKERFILE, extra_files={".dockerignore": "**/.git\n"}
    )
    _stub_run_preamble(monkeypatch)

    uploaded: list[object] = []
    monkeypatch.setattr(
        run_module,
        "upload_tasks_with_progress",
        lambda *a, **k: uploaded.append(a) or [],
    )

    result = runner.invoke(app, ["run", str(task_dir), "--agent", "claude-code"])
    assert result.exit_code == 1
    assert uploaded == [], "preflight must abort before any upload happens"


def test_run_force_proceeds_and_still_prints_findings(make_task, monkeypatch):
    task_dir = make_task(
        dockerfile=_LEAKY_DOCKERFILE, extra_files={".dockerignore": "**/.git\n"}
    )
    _stub_run_preamble(monkeypatch)

    def _sentinel(*a, **k):
        raise RuntimeError("reached upload")

    monkeypatch.setattr(run_module, "upload_tasks_with_progress", _sentinel)

    result = runner.invoke(
        app, ["run", str(task_dir), "--agent", "claude-code", "--force"]
    )
    # Getting past the gate is proved by the sentinel firing.
    assert "reached upload" in str(result.exception)
    assert "dockerfile_leaks" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_cli_preflight.py -k "run_aborts or run_force" -v`
Expected: FAIL — no `--force` option; the run proceeds to upload.

- [ ] **Step 3: Add the `--force` option**

In `oddish/src/oddish/cli/run.py`, add the import near the other CLI imports:

```python
from oddish.cli.preflight import gate_preflight
from oddish.preflight.runner import run_checks
```

Add the option to `run()`'s signature, immediately after `force_new_version` (which ends around `run.py:504`):

```python
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Submit even if preflight checks fail. Findings are still "
                "printed. Unrelated to --force-new-version."
            ),
        ),
    ] = False,
```

- [ ] **Step 4: Call the gate before upload**

In `run()`, immediately after the `resolve_local_task_paths(...)` call in the `else:` branch (ending at `run.py:828`), add:

```python
    # Gate before upload: a task that leaks its own answer should never cost a
    # trial. Unlike validate_tasks(), one bad task fails the whole run — a
    # pre-commit hook does not let 19 of 20 files through.
    if task_paths:
        gate_preflight(
            run_checks(task_paths), force=force, json_output=json_output
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_cli_preflight.py -v`
Expected: 7 passed

- [ ] **Step 6: Run the full CLI suite for regressions**

Run: `cd oddish && uv run pytest tests/ -x -q -k "cli or preflight"`
Expected: all passed

- [ ] **Step 7: Update the changelog**

Add to `CHANGELOG.md` under the unreleased heading:

```markdown
### Added

- `oddish preflight <path>` checks local tasks for integrity problems before
  they cost a trial: solution/tests baked into the agent image, repo fetches or
  `.git` directories that expose branch history, unjustified open internet,
  patch-file solutions, and brittle source-scanning anti-cheat. `--json` emits
  findings for CI.
- `oddish run` now runs preflight before upload. `--force` submits anyway and
  still prints the findings.

### Changed

- `oddish run` aborts when **any** resolved task fails preflight. Previously a
  broken task in a multi-task run was reported and silently skipped while the
  rest proceeded. Use `--force` for the old behaviour.
```

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/cli/run.py oddish/tests/test_cli_preflight.py CHANGELOG.md
git commit -m "feat(run): gate submission on preflight, add --force override"
```

---

## Manual verification

After Task 8, verify against a real task rather than a fixture:

```bash
cd oddish
uv run oddish preflight ../path/to/a/real/task
uv run oddish preflight ../path/to/a/real/task --json | jq '.findings[].check_id'
```

Expected: exit 0 with "Preflight passed", or a grouped list of findings with clickable `file:line` locations.

## Follow-ups (explicitly out of scope)

- Port the remaining general checks: `test_file_references`, `test_sh_sanity`,
  `reward_format`, `metrics_partial_score`, `artifacts`, `asset_encryption`,
  `task_absolute_path`.
- Invert the dependency: change harbor-lh's `static-checks.yml` to call
  `oddish preflight --json` instead of its own `ci_checks/*.sh`, removing the
  bash/Python divergence.
- A config layer (`[preflight]` in `oddish.toml`) for the house-policy checks.
