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
