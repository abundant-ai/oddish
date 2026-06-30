"""Side-effectful probe overlay: stage assets + rewrite instruction.md.

Shared by both runners — the local in-process runner (``local_runner``) and
the Modal/cloud worker (``workers/queue/trial_handler``) — so probe trials
behave identically in dev and production. The pure rendering logic lives in
:mod:`oddish.worker.probe_overlay`; this module adds the filesystem effects.

The caller must pass a *writable* task dir (a temp copy, never a canonical or
mounted task path) since ``apply_probe_overlay`` mutates ``instruction.md`` in
place and writes staged assets underneath it.
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
    BOUNDARY_MARKER_NAME,
    BOUNDARY_MARKER_TEXT,
    HARBOR_DIR_NAME,
    PROBE_SYSTEM_FRAMING,
    QUERY_CLI_NAME,
    render_probe_instruction,
)
from oddish.worker.skills_overlay import SkillBundle, materialize_skills

logger = logging.getLogger(__name__)


def stage_query_cli(work_task_dir: Path) -> None:
    """Copy the Node oddish-query CLI into the staged probe-harness dir, executable."""
    cli_bytes = resources.files("oddish").joinpath(f"assets/{QUERY_CLI_NAME}").read_bytes()
    dest = work_task_dir / QUERY_CLI_NAME
    dest.write_bytes(cli_bytes)
    dest.chmod(0o755)


def stage_cli_mount(harness_dir: Path) -> None:
    """Write ONLY the oddish-query CLI into ``harness_dir`` (the /probe-harness
    mount). Everything else probe-only goes to the hidden stage, so this mount is
    the single advertised entry point the agent sees."""
    harness_dir.mkdir(parents=True, exist_ok=True)
    stage_query_cli(harness_dir)


def write_boundary_markers(stage_root: Path) -> None:
    """Plant the self-describing boundary marker in the hidden stage so direct
    file discovery (an agent that greps the FS and reads a file instead of using
    the CLI) still learns this is probe-only, deliberately-hidden infrastructure.
    Root + answer-key subdirs only; ``harbor_src`` is left pristine (byte-exact)."""
    targets = [stage_root, stage_root / "solution", stage_root / "tests"]
    for d in targets:
        try:
            if d.is_dir():
                (d / BOUNDARY_MARKER_NAME).write_text(BOUNDARY_MARKER_TEXT)
        except Exception:
            logger.exception("probe: writing boundary marker in %s failed", d)


def stage_harbor_source(work_task_dir: Path) -> bool:
    """Copy the *live* harbor package source into ``work_task_dir/harbor_src``.

    Resolves harbor from the running interpreter (``harbor.__file__``) so the
    staged source is byte-for-byte the code that actually builds the env and
    scores the trial -- otherwise a bug the agent "finds" in the source might
    not exist in the live harness, making the exploit theater. The runner
    uploads the work dir to the probe-harness root, so this lands at
    :data:`HARBOR_CONTAINER_DIR`. Failures are logged and skipped; they never
    block the probe. Returns True if staged.
    """
    try:
        import harbor

        src = Path(harbor.__file__).resolve().parent
    except Exception:
        logger.exception("probe: could not locate harbor source; skipping")
        return False

    dest = work_task_dir / HARBOR_DIR_NAME
    try:
        shutil.copytree(
            src,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".git"),
            dirs_exist_ok=True,
        )
    except Exception:
        logger.exception("probe: staging harbor source failed")
        return False
    return True


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


def collect_visibility(task_dir: Path) -> list[str]:
    """List the top-level entries staged under the probe-harness root.

    Everything staged for the probe (``tests/``, ``solution/``, ``environment/``,
    the staged ``related_trials/`` + ``harbor_src/``, ``task.toml``, ...) is
    uploaded under :data:`PROBE_HARNESS_DIR` — none of it reaches the real
    agent's ``/app``. This returns those top-level entries (sorted, dirs keep a
    trailing ``/``) so the visibility map can enumerate the harness contents.
    ``instruction.md`` is excluded (it is the probe's own prompt, delivered as a
    string); hidden entries are skipped. Call this AFTER staging so the staged
    dirs are included.
    """
    reserved = {"instruction.md"}
    probe_only: list[str] = []
    for child in sorted(task_dir.iterdir(), key=lambda p: p.name):
        if child.name in reserved or child.name.startswith("."):
            continue
        probe_only.append(f"{child.name}/" if child.is_dir() else child.name)

    return probe_only


async def apply_probe_overlay(
    task_dir: Path,
    *,
    task_id: str,
    trial_id: str,
    extra_instructions: str,
    probe_scope: str = "task",
    time_budget_sec: float | None = None,
) -> None:
    """Stage probe-only assets (harbor source, boundary markers) and rewrite
    ``task_dir/instruction.md`` in place.

    The oddish-query CLI is **not** staged here: the runners deliver it
    separately to ``/probe-harness`` via :func:`stage_cli_mount`. Staging it
    into ``task_dir`` would also land a redundant, never-referenced copy in the
    hidden stage (``task_dir`` is uploaded to ``STAGE_DIR``).

    ``task_dir`` MUST be a writable temp copy of the task. Staging failures
    are logged and skipped; they never block the probe from running.

    ``probe_scope`` is retained for caller-signature stability; it no longer
    routes any staging behavior (its only consumer, related-trial-log
    selection, was removed when the probe moved to the ``oddish-query`` CLI).

    (Org skill injection is handled separately by the runners via
    ``stage_org_skills`` + ``AgentConfig.skills`` -- NOT here, because skills
    must reach the sandbox through Harbor's skill-upload path, not the task dir
    which Harbor never mounts into the container.)
    """
    # Stage harbor's own source as a reward-hack surface (read-only, in-mount,
    # network-immune). Best-effort: never blocks the probe.
    try:
        stage_harbor_source(task_dir)
    except Exception:
        logger.exception("probe: staging harbor source failed")

    instr_path = task_dir / "instruction.md"
    original = instr_path.read_text() if instr_path.exists() else ""

    # Save the real agent's brief verbatim so the probe can study it as the
    # *other* agent's instructions, and enumerate the harness contents now that
    # all staged dirs are in place.
    (task_dir / AGENT_BRIEF_NAME).write_text(original)
    probe_only = collect_visibility(task_dir)
    write_boundary_markers(task_dir)

    instr_path.write_text(
        render_probe_instruction(
            PROBE_SYSTEM_FRAMING,
            extra_instructions,
            original,
            time_budget_sec=time_budget_sec,
            probe_only_paths=probe_only,
        )
    )
