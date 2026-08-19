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
# subtrees, so this stages the artifact into the verifier dir and grades that
# it exists, parses, and has the required keys -- a nonzero exit fails the
# verifier and lets normal trial retries re-run the agent. Full schema
# validation stays host-side in the importer.
_ANALYSIS_TEST_SH = """#!/bin/sh
OUT="${{HARBOR_VERIFIER_LOG_DIR:-/logs/verifier}}"
mkdir -p "$OUT"
SRC="/logs/{artifact}"
KEYS="{required_keys}"
if [ ! -s "$SRC" ]; then
  echo "the agent did not write /logs/{artifact}" | tee "$OUT/error.txt" >&2
  exit 1
fi
cp "$SRC" "$OUT/{artifact}"
python3 -c 'import json,sys
data = json.load(open(sys.argv[1]))
missing = [k for k in sys.argv[2].split() if k not in data]
if missing:
    raise SystemExit("missing required keys: %s" % " ".join(missing))
' "$SRC" "$KEYS" 2>"$OUT/error.txt" || exit 1
echo "1.0" > "$OUT/reward.txt"
exit 0
"""

# Top-level keys the artifact must carry to be worth importing. Kept
# minimal on purpose: the importer's Pydantic models are the authority,
# this only stops an empty or wrong-shaped file from earning reward 1.0.
_REQUIRED_KEYS = {
    "qa_result.json": "trials verdict",
    "audit_result.json": "items",
}


# An analysis trial runs on OUR task image, not the audited one: that image
# is an unknown (may lack python/node/network) and its verifier grades
# task-solving. The agent reads the audited task via the oddish-query CLI.
_ANALYSIS_DOCKERFILE = """FROM python:3.13-slim
RUN apt-get update \\
    && apt-get install -y --no-install-recommends nodejs curl procps ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
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


def apply_analysis_overlay(work_task_dir: Path, *, brief: str, artifact: str) -> None:
    """Replace the staged task with the analysis task: the brief as the
    instruction, our image, and the artifact verifier as the tests. Nothing
    of the audited task remains -- its trials, logs, and files reach the
    agent through the oddish-query CLI, the same way the gold harness
    audits from artifacts."""
    import shutil

    for child in list(work_task_dir.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

    (work_task_dir / "instruction.md").write_text(brief)
    (work_task_dir / "task.toml").write_text(_ANALYSIS_TASK_TOML)
    env_dir = work_task_dir / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text(_ANALYSIS_DOCKERFILE)
    tests_dir = work_task_dir / "tests"
    tests_dir.mkdir(parents=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(
        _ANALYSIS_TEST_SH.format(
            artifact=artifact,
            required_keys=_REQUIRED_KEYS[artifact],
        )
    )
    test_sh.chmod(0o755)


def stage_cli_mount(harness_dir: Path) -> None:
    """Write ONLY the oddish-query CLI into ``harness_dir`` (the /probe-harness
    mount). Everything else probe-only goes to the hidden stage, so this mount is
    the single advertised entry point the agent sees."""
    harness_dir.mkdir(parents=True, exist_ok=True)
    stage_query_cli(harness_dir)


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
