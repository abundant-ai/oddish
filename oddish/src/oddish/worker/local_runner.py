"""Local in-process trial runner. Used when ``ODDISH_LOCAL_MODE=1``.

Bypasses the Modal queue and runs trials directly via Harbor's Python
API, talking to a local Docker daemon for the env. State is written to
the same Postgres rows the Modal worker would update, so the rest of
the stack (FE, analysis pipeline) sees a normal trial.

Task 8 wires ``_run_harbor_trial`` to actually invoke Harbor and adds
the probe task-mutation overlay: when ``harbor_config.extra_instructions``
is set, the runner copies the task dir to a temp work dir, prepends the
operator's prompt to ``instruction.md``, and points Harbor at the temp
copy. Harbor itself stays unpatched -- it just sees a normal task with a
modified ``instruction.md``. Mirrors the long-horizon ``/cheat`` CI
workflow which ``cat``s the cheating prompt into ``instruction.md``
before submitting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from harbor.trial.trial import Trial

from oddish.config import (
    ZAI_DEFAULT_BASE_URL,
    is_zai_model,
    zai_bare_model_id,
)
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    get_session,
)
from oddish.db.storage import resolve_task_directory
from oddish.worker.probe_analysis import (
    extract_probe_artifacts,
    run_probe_analyzer,
)
from oddish.worker.probe_staging import apply_probe_overlay, stage_org_skills
from oddish.worker.local_offline_policy import enable_local_internet, task_is_offline
from oddish.task_timeouts import PROBE_AGENT_TIMEOUT_SEC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CPU watchdog: a bash script that runs inside the trial container and kills
# runaway non-claude processes before they consume the trial's wall-clock
# budget. Invoked via ``docker exec -d`` from ``_watchdog_task`` once the
# container is up.
#
# Safe-pattern list is conservative -- better to miss a kill than to kill the
# agent itself (claude / tee / the watchdog). Any process whose argv contains
# one of these substrings is skipped.
# ---------------------------------------------------------------------------
_WATCHDOG_SAFE_PATTERNS = (
    "claude",  # the agent itself
    "tee /logs",  # Harbor's stdout redirect for the agent log
    "watchdog",  # this script (don't kill yourself)
    "sleep",  # benign
    "ps ",  # this poll's own ps invocation
)

_WATCHDOG_SCRIPT = r"""
LOG=/logs/watchdog.log
mkdir -p /logs
echo "[watchdog] started pid=$$ at $(date -Is)" >> "$LOG"

# Process state: pid -> seconds_above_threshold (tracked in /tmp/watchdog_state)
STATE_DIR=/tmp/watchdog_state
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"

CPU_THRESHOLD=90
TIME_THRESHOLD=300
POLL_INTERVAL=30

while true; do
    NOW=$(date +%s)
    # ps: output pid, %cpu (current), elapsed seconds, command
    ps -eo pid=,pcpu=,etimes=,args= 2>/dev/null | while read pid cpu etime args; do
        # Skip kernel threads (no args) and our own watchdog
        [ -z "$args" ] && continue
        # Skip safe patterns
        skip=0
        for pat in claude "tee /logs" watchdog "ps -eo" sleep; do
            case "$args" in *"$pat"*) skip=1; break ;; esac
        done
        [ "$skip" -eq 1 ] && continue

        # Strip decimal from cpu
        cpu_int=${cpu%.*}
        [ -z "$cpu_int" ] && continue

        if [ "$cpu_int" -gt "$CPU_THRESHOLD" ] 2>/dev/null && \
           [ "$etime" -gt "$TIME_THRESHOLD" ] 2>/dev/null; then
            # Avoid double-killing
            if [ -f "$STATE_DIR/$pid" ]; then continue; fi
            touch "$STATE_DIR/$pid"
            echo "[watchdog] killing pid=$pid cpu=$cpu etime=$etime args=$(echo "$args" | head -c 200)" >> "$LOG"
            kill -TERM "$pid" 2>/dev/null
            sleep 2
            # If still alive, SIGKILL
            if kill -0 "$pid" 2>/dev/null; then
                echo "[watchdog] SIGKILL pid=$pid (didn't respond to TERM)" >> "$LOG"
                kill -KILL "$pid" 2>/dev/null
            fi
        fi
    done
    sleep "$POLL_INTERVAL"
done
"""


async def _find_trial_container(
    trial_name: str, *, timeout: float = 60.0
) -> str | None:
    """Poll ``docker ps`` until a container matching the trial name appears.

    Harbor's docker-compose project naming produces a container named
    ``<trial_name_lowercased>-main-1``. We match by suffix (case-insensitive)
    so any compose-project prefix Harbor adds in the future still works.

    Returns the container name, or None if not found within the timeout.
    """
    expected_suffix = f"{trial_name.lower()}-main-1"
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--format",
                "{{.Names}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
        except Exception as exc:
            logger.warning("watchdog: docker ps failed: %s", exc)
            await asyncio.sleep(2)
            continue

        names = stdout.decode().strip().splitlines()
        for name in names:
            if name.lower().endswith(expected_suffix):
                return name
        await asyncio.sleep(2)
    return None


async def _start_watchdog(container_name: str) -> bool:
    """Exec the watchdog script into the container as a detached process.

    Uses ``docker exec -d`` so the bash process keeps running after this
    helper returns. The watchdog terminates naturally when the container is
    torn down. Returns True if the exec call succeeded.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-d",
            container_name,
            "bash",
            "-c",
            _WATCHDOG_SCRIPT,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()  # ``-d`` returns quickly
        logger.info("watchdog started in container %s", container_name)
        return True
    except Exception as exc:
        logger.warning("watchdog: failed to start in %s: %s", container_name, exc)
        return False


async def _watchdog_task(trial_name: str) -> None:
    """Background task: wait for the container, then start the watchdog."""
    container_name = await _find_trial_container(trial_name)
    if container_name is None:
        logger.warning("watchdog: container for trial '%s' never appeared", trial_name)
        return
    await _start_watchdog(container_name)


_BEDROCK_REGION_PREFIXES = ("global.", "us.", "eu.", "apac.", "apn.")

# Cap how long a local probe agent may run. Shared with the cloud runner so dev
# and prod cap probes identically; see ``oddish.task_timeouts`` for the full
# rationale. Overridable via ODDISH_PROBE_AGENT_TIMEOUT_SEC.
_PROBE_AGENT_TIMEOUT_SEC = PROBE_AGENT_TIMEOUT_SEC


def _strip_nul(obj: object) -> object:
    """Recursively strip NUL (``\\u0000``) chars from a JSON-able structure.

    Postgres text/jsonb cannot store ``\\u0000``; agent output occasionally
    contains one (e.g. raw bytes echoed into a transcript), which made the
    ``trial.result`` / ``trial.analysis`` write fail with asyncpg
    ``UntranslatableCharacterError`` and dropped the whole trial.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(v) for v in obj]
    return obj


def _bedrock_agent_env(model_name: str | None) -> dict[str, str]:
    """Env that lets Claude Code invoke a Bedrock-routed model in local dev.

    Returns ``{}`` for non-Bedrock models. For a ``global.anthropic.*`` (or
    other region-profile) id, sets ``CLAUDE_CODE_USE_BEDROCK`` and forwards the
    host's AWS creds into the agent container, defaulting the region. The
    ambient ``ANTHROPIC_API_KEY`` is blanked so the Bedrock route wins (same
    move as the OpenRouter path in the cloud worker).
    """
    model_lc = (model_name or "").strip().lower()
    is_bedrock = ".anthropic." in model_lc and any(
        model_lc.startswith(p) for p in _BEDROCK_REGION_PREFIXES
    )
    if not is_bedrock:
        return {}

    env: dict[str, str] = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        val = os.environ.get(var)
        if val:
            env[var] = val
    env.setdefault("AWS_REGION", "us-east-1")
    env["ANTHROPIC_API_KEY"] = ""
    return env


def _zai_agent_env(model_name: str | None) -> dict[str, str]:
    """Env that lets Claude Code reach z.ai's GLM endpoint in local dev.

    Returns ``{}`` for non-GLM models. Mirrors ``harbor_runner``'s z.ai env:
    point Claude Code at the z.ai base URL, forward the host's ``ZAI_API_KEY``
    as the auth token, pin the bare GLM id, and blank the ambient Bedrock creds
    so the z.ai route wins.
    """
    if not is_zai_model(model_name):
        return {}

    bare_model = zai_bare_model_id(model_name or "")
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": os.environ.get("ZAI_BASE_URL") or ZAI_DEFAULT_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": os.environ.get("ZAI_API_KEY", ""),
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_USE_BEDROCK": "",
        "AWS_BEARER_TOKEN_BEDROCK": "",
    }
    if bare_model:
        env["ANTHROPIC_MODEL"] = bare_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = bare_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = bare_model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = bare_model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = bare_model
    return env


async def run_trial_locally(trial_id: str, *, dry_run: bool = False) -> None:
    """Execute a probe trial in-process and mirror status to the DB.

    Status transitions: ``QUEUED`` -> ``RUNNING`` -> ``SUCCESS``
    (or ``FAILED`` on exception, with ``error_message`` populated).

    When ``dry_run`` is True, skips the actual Harbor call. Used in
    tests to exercise the status-transition path without spinning up
    Docker.
    """
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(f"Trial {trial_id} not found")
        trial.status = TrialStatus.RUNNING
        trial.started_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> RUNNING", trial_id)

    try:
        if not dry_run:
            await _run_harbor_trial(trial_id)
    except Exception as exc:
        logger.exception("local_runner: trial %s failed", trial_id)
        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is not None:
                trial.status = TrialStatus.FAILED
                trial.error_message = str(exc)
                trial.finished_at = datetime.now(timezone.utc)
        raise

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(
                f"Trial {trial_id} disappeared mid-run; cannot mark SUCCESS"
            )
        trial.status = TrialStatus.SUCCESS
        trial.finished_at = datetime.now(timezone.utc)
        logger.info("local_runner: trial %s -> SUCCESS", trial_id)


async def _run_harbor_trial(trial_id: str) -> None:
    """Execute the trial against a local Harbor instance.

    Reads the trial row + linked task to get ``task_path``, ``agent``,
    ``model`` and ``harbor_config``. If ``harbor_config.extra_instructions``
    is set, copies the task dir to a temp work dir and prepends the
    operator prompt to ``instruction.md`` so Harbor sees the mutated
    instruction without any Harbor-side patch. Persists ``reward`` and
    the full ``TrialResult`` JSON back to the trial row, then cleans up
    the temp work dir.
    """
    from harbor.models.trial.config import (
        AgentConfig,
        TaskConfig,
        TrialConfig,
    )

    # ---------------------------------------------------------------
    # Load trial + task. ``selectin`` would normally hydrate
    # ``trial.task`` eagerly, but we hit it via ``session.get(TaskModel, ...)``
    # explicitly to mirror the canonical pattern used by the Modal trial
    # handler (oddish/workers/queue/trial_handler.py).
    # ---------------------------------------------------------------
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            raise ValueError(f"Trial {trial_id} not found")
        task = await session.get(TaskModel, trial.task_id)
        if task is None:
            raise ValueError(
                f"Trial {trial_id} references missing task {trial.task_id}"
            )
        task_path_str = task.task_path
        task_s3_key = task.task_s3_key
        task_db_id = task.id
        harbor_config = trial.harbor_config or {}
        agent_name = trial.agent
        model_name = trial.model
        trial_org_id = trial.org_id
        extra_instructions = harbor_config.get("extra_instructions")

    # Resolve the task files. Cloud-created tasks store their files in S3
    # (MinIO in local dev) with a ``s3://`` task_path, so a bare ``Path``
    # never exists on disk. ``resolve_task_directory`` downloads from S3 when
    # needed and falls back to a local path -- the same helper the Modal
    # trial handler uses, so local and cloud resolve tasks identically.
    # ``task_temp_dir`` is non-None only for S3 downloads and must be cleaned.
    task_path, task_temp_dir, _ = await resolve_task_directory(
        task_id=task_db_id,
        task_s3_key=task_s3_key,
        task_path=task_path_str,
    )

    # ---------------------------------------------------------------
    # Probe overlay: copy the task dir to a temp work dir and prepend
    # the operator's directive (with a system framing) to instruction.md.
    # Harbor reads the modified file from the copy without any patch.
    # The framing reorients the agent: the operator's directive is the
    # goal, the original task is context only.
    # ---------------------------------------------------------------
    work_root: Path | None = None
    # Host dirs (one skills-root) handed to Harbor via ``AgentConfig.skills``;
    # Harbor uploads each ``<name>/`` skill into the sandbox and the claude-code
    # agent registers them so the agent discovers them.
    agent_skill_paths: list[Path] = []
    if extra_instructions:
        work_root = Path(tempfile.mkdtemp(prefix=f"probe-{trial_id}-"))
        work_task_dir = work_root / task_path.name
        shutil.copytree(task_path, work_task_dir, symlinks=True)
        # Prepend the operator directive, append the test/related-log
        # sections, and pre-stage prior real-attempt logs. Shared with the
        # cloud worker so probes behave identically in dev and production.
        await apply_probe_overlay(
            work_task_dir,
            task_id=task_db_id,
            trial_id=trial_id,
            extra_instructions=extra_instructions,
            probe_scope=harbor_config.get("probe_scope"),
            target_trial_id=harbor_config.get("probe_target_trial_id"),
            time_budget_sec=_PROBE_AGENT_TIMEOUT_SEC,
        )
        actual_task_path = work_task_dir
        # Stage the org's shared skills into a root under work_root and hand it
        # to Harbor below. Best-effort; never blocks the probe.
        skills_root = work_root / "agent_skills"
        n_skills = await stage_org_skills(skills_root, org_id=trial_org_id)
        if n_skills:
            agent_skill_paths = [skills_root]
            logger.info(
                "probe: staged %d skill(s) for trial %s", n_skills, trial_id
            )
    else:
        actual_task_path = task_path

    # ---------------------------------------------------------------
    # Build Harbor configs and run the trial.
    # ---------------------------------------------------------------
    trials_dir = Path(f"/tmp/oddish-local-trials/{trial_id}")
    trials_dir.mkdir(parents=True, exist_ok=True)

    # oddish routes Claude through AWS Bedrock (model ids like
    # ``global.anthropic.claude-...``). In the cloud the Modal image supplies
    # the AWS creds + ``CLAUDE_CODE_USE_BEDROCK`` ambiently; locally nothing
    # does, so forward the host's AWS creds into the agent container -- without
    # this the in-container ``claude`` CLI has no way to invoke the Bedrock id
    # and exits 1. Mirrors ``harbor_runner._apply_claude_code_openrouter_env``.
    # Offline tasks run network_mode:none under Harbor's Docker env, which
    # blocks the model API (Bedrock) the agent must call -- so the agent can't
    # run locally. Prod (Modal) reaches Bedrock via a domain allowlist; local
    # Docker has no such primitive. For LOCAL runs, relax the constraint so the
    # container has egress and Harbor's normal install + the agent both work.
    # Trades offline isolation for a working local run; prod keeps real
    # isolation. Modal/cloud path untouched.
    if work_root is not None and task_is_offline(actual_task_path):
        if enable_local_internet(actual_task_path):
            logger.info(
                "probe: local offline task %s -- enabled internet so the agent "
                "can reach Bedrock (isolation relaxed locally only)",
                trial_id,
            )

    agent_config = AgentConfig(
        name=agent_name,
        model_name=model_name,
        override_timeout_sec=_PROBE_AGENT_TIMEOUT_SEC,
        skills=agent_skill_paths,
    )
    bedrock_env = _bedrock_agent_env(model_name)
    if bedrock_env:
        agent_config.env = {**(agent_config.env or {}), **bedrock_env}
    zai_env = _zai_agent_env(model_name)
    if zai_env:
        agent_config.env = {**(agent_config.env or {}), **zai_env}

    cfg = TrialConfig(
        task=TaskConfig(path=actual_task_path),
        agent=agent_config,
        trials_dir=trials_dir,
    )

    # ``Trial.__init__`` requires a pre-loaded ``Task`` and is marked
    # deprecated; ``Trial.create`` is the supported entrypoint.
    # Tests monkeypatch ``worker.local_runner.Trial`` -- the fake
    # exposes a matching ``create`` classmethod so the call shape stays
    # identical between the real Harbor Trial and the test double.
    harbor_trial = await Trial.create(cfg)

    # Start the CPU watchdog in parallel -- it polls for the container by
    # name, then docker-execs a polling script inside that kills runaway
    # non-claude processes pegged >90% CPU for >300s. Detached inside the
    # container, so it dies naturally when the container is torn down.
    watchdog_bg = asyncio.create_task(_watchdog_task(cfg.trial_name))

    try:
        await harbor_trial.run()
    finally:
        if work_root is not None:
            shutil.rmtree(work_root, ignore_errors=True)
        if task_temp_dir is not None:
            shutil.rmtree(task_temp_dir, ignore_errors=True)
        # Watchdog is detached inside the container; nothing to clean up
        # there. We only need to cancel our launcher task in case the
        # container never came up (so it isn't left polling forever).
        if not watchdog_bg.done():
            watchdog_bg.cancel()
            try:
                await watchdog_bg
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------------------------------------------------------
    # Read structured artifacts off disk + run the probe analyzer,
    # then persist reward + result + analysis back to the trial row.
    # ---------------------------------------------------------------
    result = harbor_trial.result

    # Pull structured artifacts (trajectory, verifier stdout, agent timeline,
    # watchdog log) off disk via the shared probe extractor -- the same helper
    # the cloud analysis worker uses, so the result page + analyzer see
    # identical inputs in dev and production.
    artifacts = extract_probe_artifacts(trials_dir)
    agent_messages = artifacts["agent_messages"]
    verifier_stdout = artifacts["verifier_stdout"]

    # Build the result payload to persist.
    result_payload: dict = {}
    if result is not None and hasattr(result, "model_dump"):
        try:
            result_payload = result.model_dump(mode="json")
        except TypeError:
            # Some MagicMock-style stubs don't accept ``mode``.
            result_payload = result.model_dump()
    if not isinstance(result_payload, dict):
        result_payload = {}
    result_payload["_artifacts"] = artifacts

    # Compute reward up-front (we need it both for the analyzer and to persist).
    verifier_result = getattr(result, "verifier_result", None) if result else None
    rewards = getattr(verifier_result, "rewards", None) if verifier_result else None
    reward_value: float | None = None
    if rewards:
        raw_reward = rewards.get("reward")
        if raw_reward is not None:
            try:
                reward_value = float(raw_reward)
            except (TypeError, ValueError):
                reward_value = None

    # Run the LLM analyzer.
    extra_instructions = harbor_config.get("extra_instructions") or ""
    result_focus = harbor_config.get("result_focus") or ""
    evaluation_metric = harbor_config.get("evaluation_metric") or "none"
    ratio_unit = harbor_config.get("ratio_unit")
    ratio_verb = harbor_config.get("ratio_verb")
    analyzer_summary: dict | None = None
    analyzer_status = AnalysisStatus.FAILED
    analyzer_error: str | None = None
    analysis_started_at = datetime.now(timezone.utc)
    try:
        analyzer_summary = await run_probe_analyzer(
            extra_instructions=extra_instructions,
            agent_messages=agent_messages,
            verifier_stdout=verifier_stdout or "",
            reward=reward_value,
            result_focus=result_focus,
            evaluation_metric=evaluation_metric,
            ratio_unit=ratio_unit,
            ratio_verb=ratio_verb,
        )
        analyzer_status = AnalysisStatus.SUCCESS
    except Exception as exc:
        analyzer_error = str(exc)
        logger.exception("Probe analyzer failed for trial %s", trial_id)
    analysis_finished_at = datetime.now(timezone.utc)

    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if trial is None:
            return
        trial.harbor_result_path = str(trials_dir)
        if reward_value is not None:
            trial.reward = reward_value
        trial.result = _strip_nul(result_payload)
        if analyzer_summary is not None:
            trial.analysis = _strip_nul(analyzer_summary)
        trial.analysis_status = analyzer_status
        trial.analysis_error = analyzer_error
        trial.analysis_started_at = analysis_started_at
        trial.analysis_finished_at = analysis_finished_at
