"""Side-effectful probe overlay: stage assets + rewrite instruction.md.

Shared by both runners — the local in-process runner (``local_runner``) and
the Modal/cloud worker (``workers/queue/trial_handler``) — so probe trials
behave identically in dev and production. The pure rendering logic lives in
:mod:`oddish.worker.probe_overlay`; this module adds the filesystem effects.

The caller must pass a *writable* task dir (a temp copy, never a canonical or
mounted task path) since ``apply_probe_overlay`` mutates ``instruction.md`` in
place.
"""

from __future__ import annotations

import logging
import shutil
from importlib import resources
from pathlib import Path

from oddish.core.skills import list_skills_core
from oddish.db import get_session
from oddish.worker.probe_overlay import (
    AGENT_BRIEF_NAME,
    QUERY_CLI_NAME,
    render_probe_instruction,
)
from oddish.worker.skills_overlay import SkillBundle, materialize_skills

logger = logging.getLogger(__name__)

_ANALYSIS_CONTRACT_DIR = ".analysis-contract"
_ANALYSIS_SUBMIT_COMMAND = "submit-analysis-result"


def read_query_cli_text() -> str:
    """Return the oddish-query CLI source (for Modal instantiation injection)."""
    return resources.files("oddish").joinpath(f"assets/{QUERY_CLI_NAME}").read_text()


def stage_query_cli(work_task_dir: Path) -> None:
    """Copy the Node oddish-query CLI into the staged probe-harness dir, executable."""
    cli_bytes = (
        resources.files("oddish").joinpath(f"assets/{QUERY_CLI_NAME}").read_bytes()
    )
    dest = work_task_dir / QUERY_CLI_NAME
    dest.write_bytes(cli_bytes)
    dest.chmod(0o755)


# The analysis verifier. Harbor only collects the agent/ and verifier/
# subtrees, so this stages the artifact into the verifier dir and validates
# it against the contract the host pinned at trial creation (expected.json,
# checked by the staged copy of oddish.worker.analysis_result_check) -- a
# nonzero exit fails the verifier and lets normal trial retries re-run the
# agent. The importer runs the same validator with the same payload, so an
# artifact the verifier passed cannot be refused as malformed later, and an
# incomplete one never earns reward 1.0 here.
_ANALYSIS_TEST_SH = """#!/bin/sh
OUT="${{HARBOR_VERIFIER_LOG_DIR:-/logs/verifier}}"
mkdir -p "$OUT"
SRC="/logs/{artifact}"
TESTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -s "$SRC" ]; then
  if [ -s "/logs/qa_submission_error.txt" ]; then
    if [ -s "/logs/qa_result-rejected.json" ]; then
      cp "/logs/qa_result-rejected.json" "$OUT/qa_result-rejected.json"
    fi
    {{ echo "QA artifact validation failed:"; cat "/logs/qa_submission_error.txt"; }} \
      | tee "$OUT/error.txt" >&2
  else
    echo "the agent did not write /logs/{artifact}" | tee "$OUT/error.txt" >&2
  fi
  exit 1
fi
cp "$SRC" "$OUT/{artifact}"
python3 "$TESTS_DIR/analysis_result_check.py" "$SRC" "$TESTS_DIR/expected.json" 2>"$OUT/error.txt" || exit 1
echo "1.0" > "$OUT/reward.txt"
exit 0
"""


_ANALYSIS_SUBMIT_SH = """#!/bin/sh
set -u

if [ "$#" -ne 1 ]; then
  echo "usage: /probe-harness/submit-analysis-result <draft.json>" >&2
  exit 2
fi

SRC="$1"
HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONTRACT_DIR="$HARNESS_DIR/.analysis-contract"
LOG_DIR="${{ODDISH_ANALYSIS_LOG_DIR:-/logs}}"
ATTEMPTS_FILE="${{ODDISH_ANALYSIS_ATTEMPTS_FILE:-/tmp/oddish-analysis-submit-attempts}}"
mkdir -p "$LOG_DIR"
ATTEMPTS=0
if [ -f "$ATTEMPTS_FILE" ]; then
  ATTEMPTS=$(cat "$ATTEMPTS_FILE" 2>/dev/null || echo 0)
fi
case "$ATTEMPTS" in
  ''|*[!0-9]*) ATTEMPTS=0 ;;
esac
ATTEMPTS=$((ATTEMPTS + 1))
echo "$ATTEMPTS" > "$ATTEMPTS_FILE"

if [ "$ATTEMPTS" -gt 3 ]; then
  echo "submission limit reached: one initial submission and two repairs" \
    | tee "$LOG_DIR/qa_submission_error.txt" >&2
  exit 1
fi
if [ ! -s "$SRC" ]; then
  echo "analysis draft is missing or empty: $SRC" \
    | tee "$LOG_DIR/qa_submission_error.txt" >&2
  exit 1
fi

ERROR_FILE="/tmp/oddish-analysis-submit-error.txt"
if ! python3 "$CONTRACT_DIR/analysis_result_check.py" \
  "$SRC" "$CONTRACT_DIR/expected.json" 2>"$ERROR_FILE"; then
  cp "$ERROR_FILE" "$LOG_DIR/qa_submission_error.txt"
  cp "$SRC" "$LOG_DIR/qa_result-rejected.json"
  echo "QA artifact validation failed (submission $ATTEMPTS of 3):" >&2
  cat "$ERROR_FILE" >&2
  exit 1
fi

rm -f "$LOG_DIR/qa_submission_error.txt"
rm -f "$LOG_DIR/qa_result-rejected.json"
cp "$SRC" "$LOG_DIR/{artifact}"
echo "QA artifact accepted and published to /logs/{artifact}"
"""


# An analysis trial runs on OUR task image, not the audited one: that image
# is an unknown (may lack python/node/network) and its verifier grades
# task-solving. QA and audit read stored data through oddish-query. Summarize
# receives a bounded prompt from the worker and only needs Python for validation.
_ANALYSIS_DOCKERFILE = """FROM python:3.13-slim
RUN apt-get update \\
    && apt-get install -y --no-install-recommends nodejs curl procps ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
"""

_SINGLE_LLM_ANALYSIS_DOCKERFILE = """FROM python:3.13-slim
WORKDIR /app
"""

_ANALYSIS_TASK_TOML = """[metadata]
name = "oddish-analysis"

[agent]
timeout_sec = 3600

[environment]
build_timeout_sec = 1200

[verifier]
timeout_sec = 60
"""


def apply_analysis_overlay(
    work_task_dir: Path,
    *,
    brief: str,
    artifact: str,
    check_payload: dict,
    needs_query_cli: bool = True,
) -> None:
    """Replace the staged task with the analysis task: the brief as the
    instruction, our image, and the artifact verifier as the tests. Nothing
    of the audited task remains. QA and audit fetch its trials, logs, and files
    through oddish-query; summarize receives its target trajectory in ``brief``.

    ``check_payload`` is the artifact contract for this trial
    (``analysis_check_payload``): it is staged as ``tests/expected.json``
    beside a copy of the shared validator so the verifier enforces exactly
    what the host importer will require."""
    import inspect
    import json
    from oddish.worker import analysis_result_check

    for child in list(work_task_dir.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

    (work_task_dir / "instruction.md").write_text(brief)
    (work_task_dir / "task.toml").write_text(_ANALYSIS_TASK_TOML)
    env_dir = work_task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text(
        _ANALYSIS_DOCKERFILE if needs_query_cli else _SINGLE_LLM_ANALYSIS_DOCKERFILE
    )
    tests_dir = work_task_dir / "tests"
    tests_dir.mkdir(parents=True)
    expected_text = json.dumps(check_payload, indent=1)
    validator_text = inspect.getsource(analysis_result_check)
    (tests_dir / "expected.json").write_text(expected_text)
    (tests_dir / "analysis_result_check.py").write_text(validator_text)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(_ANALYSIS_TEST_SH.format(artifact=artifact))
    test_sh.chmod(0o755)

    if artifact == "qa_result.json":
        contract_dir = work_task_dir / _ANALYSIS_CONTRACT_DIR
        contract_dir.mkdir()
        (contract_dir / "expected.json").write_text(expected_text)
        (contract_dir / "analysis_result_check.py").write_text(validator_text)
        submit = work_task_dir / _ANALYSIS_SUBMIT_COMMAND
        submit.write_text(_ANALYSIS_SUBMIT_SH.format(artifact=artifact))
        submit.chmod(0o755)


def stage_cli_mount(
    harness_dir: Path, *, analysis_task_dir: Path | None = None
) -> None:
    """Stage the probe CLI and, for QA tasks, their pinned submission contract."""
    harness_dir.mkdir(parents=True, exist_ok=True)
    stage_query_cli(harness_dir)
    if analysis_task_dir is None:
        return
    submit = analysis_task_dir / _ANALYSIS_SUBMIT_COMMAND
    contract = analysis_task_dir / _ANALYSIS_CONTRACT_DIR
    if not submit.is_file() or not contract.is_dir():
        return
    shutil.copy2(submit, harness_dir / _ANALYSIS_SUBMIT_COMMAND)
    shutil.copytree(contract, harness_dir / _ANALYSIS_CONTRACT_DIR)


async def stage_org_skills(
    skills_root: Path, *, org_id: str | None, skill_ids: list[str] | None = None
) -> int:
    """Materialize only the **selected** org skills (``skill_ids``) under ``skills_root/<name>/<relative_path>``.
    A skill's bundle reaches a probe only when explicitly selected at launch; ``None``/empty mounts nothing.

    ``skills_root`` is meant to be passed to Harbor as an ``AgentConfig.skills``
    entry: Harbor's ``resolve_skills`` accepts a root whose every child dir holds
    a ``SKILL.md`` (exactly this layout), uploads each ``<name>/`` skill into the
    sandbox, and the claude-code agent registers them into Claude's config dir so
    the agent discovers them. Skills are written from Postgres at stage time, so
    they reach the (network-isolated) sandbox without the agent fetching anything.

    Best-effort and per-skill resilient: a DB failure stages nothing, and one
    malformed skill is skipped without dropping the others. Returns the number
    of skills actually staged; never raises.
    """
    if not skill_ids:
        return 0
    try:
        async with get_session() as session:
            skills = await list_skills_core(session, org_id=org_id)
            wanted = set(skill_ids)
            skills = [s for s in skills if s.id in wanted]
            bundles = [
                SkillBundle(
                    name=s.name,
                    files=[(f.relative_path, f.content) for f in s.files],
                )
                for s in skills
            ]
    except Exception:
        logger.exception("probe: loading org skills failed")
        return 0

    staged = 0
    for bundle in bundles:
        try:
            materialize_skills([bundle], skills_root)
            staged += 1
        except Exception:
            logger.exception("probe: staging skill %r failed", bundle.name)
    return staged


async def apply_probe_overlay(
    task_dir: Path,
    *,
    task_id: str,
    trial_id: str,
    extra_instructions: str,
    probe_scope: str = "task",
    time_budget_sec: float | None = None,
) -> None:
    """Save AGENT_BRIEF.md and rewrite instruction.md in place. Stages nothing.

    The oddish-query CLI is delivered separately to ``/probe-harness`` via
    :func:`stage_cli_mount`. All probe-only material (solution, tests, harbor
    source) is served through the CLI from the hidden stage — none of it is
    staged into ``task_dir``.

    ``task_dir`` MUST be a writable temp copy of the task.

    ``probe_scope`` is retained for caller-signature stability; it no longer
    routes any staging behavior.

    (Org skill injection is handled separately by the runners via
    ``stage_org_skills`` + ``AgentConfig.skills`` -- NOT here, because skills
    must reach the sandbox through Harbor's skill-upload path, not the task dir
    which Harbor never mounts into the container.)
    """
    instr_path = task_dir / "instruction.md"
    original = instr_path.read_text() if instr_path.exists() else ""
    (task_dir / AGENT_BRIEF_NAME).write_text(original)
    instr_path.write_text(
        render_probe_instruction(
            "",  # framing unused
            extra_instructions,
            original,
            time_budget_sec=time_budget_sec,
            probe_only_paths=None,
        )
    )
