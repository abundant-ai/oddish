from __future__ import annotations

import asyncio
import json
import os
from builtins import ExceptionGroup
from collections import namedtuple
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from oddish.task_timeouts import TaskTimeoutValidationError  # noqa: E402
from oddish.workers import harbor_runner  # noqa: E402
from oddish.workers.codex_agent import AzureCompatibleCodex, OddishCodex  # noqa: E402
from oddish.workers.queue import trial_handler  # noqa: E402

_DISK_USAGE = namedtuple("DiskUsage", ["total", "used", "free"])


def test_check_local_storage_preflight_reports_low_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=9, free=1),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_files=100_000, f_favail=10_000, f_ffree=10_000),
    )

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "Insufficient local storage" in error
    assert "minimum 5.0GB required" in error


def test_check_local_storage_preflight_reports_low_inodes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_files=100_000, f_favail=12, f_ffree=12),
    )

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "inodes" in error
    assert "minimum 1024 required" in error


def test_check_local_storage_preflight_skips_inode_check_when_no_table(
    monkeypatch, tmp_path
):
    """Modal's ephemeral /tmp reports f_files == 0; that is unlimited, not 0 free."""
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_files=0, f_favail=0, f_ffree=0),
    )

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is None


def test_check_local_storage_preflight_reports_probe_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_runner.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_runner.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_runner.os,
        "statvfs",
        lambda path: SimpleNamespace(f_files=100_000, f_favail=10_000, f_ffree=10_000),
    )

    real_write_text = Path.write_text

    def _fail_probe_write(self: Path, *args, **kwargs):
        if self.name == "probe.txt":
            raise OSError(28, "No space left on device")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_probe_write)

    error = harbor_runner._check_local_storage_preflight(
        tmp_path / "harbor",
        include_temp_root=True,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is not None
    assert "probe failed" in error
    assert "No space left on device" in error


def test_check_local_storage_preflight_skips_temp_root_when_not_requested(
    monkeypatch, tmp_path
):
    jobs_dir = tmp_path / "harbor"
    temp_root = tmp_path / "tmp"
    seen_paths: list[Path] = []

    def _record_probe(path: Path, **_: object) -> None:
        seen_paths.append(path)
        return None

    monkeypatch.setattr(harbor_runner.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(harbor_runner, "_probe_storage_root", _record_probe)

    error = harbor_runner._check_local_storage_preflight(
        jobs_dir,
        include_temp_root=False,
        min_required_gb=5.0,
        min_required_inodes=1024,
    )

    assert error is None
    assert seen_paths == [jobs_dir.resolve()]


def test_format_exception_message_includes_exception_group_children():
    exc = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [RuntimeError("modal image build failed")],
    )

    message = harbor_runner._format_exception_message(exc)

    assert "ExceptionGroup: unhandled errors in a TaskGroup" in message
    assert "RuntimeError: modal image build failed" in message


def test_store_trial_results_marks_modal_image_build_failed_permanent(monkeypatch):
    trial = SimpleNamespace(
        task_id="task-1",
        status=trial_handler.TrialStatus.RUNNING,
        attempts=1,
        max_attempts=6,
        error_message=None,
        harbor_stage="starting",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
    )

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(trial_id: str, *, allow_missing: bool = False):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
    )
    monkeypatch.setattr(
        queue_module, "enqueue_analysis_worker_job", _fake_enqueue_analysis_worker_job
    )

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="Harbor job execution failed: RuntimeError: Image build for im-abc123 failed",
        exit_code=-1,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.FAILED
    assert trial.harbor_stage == "image_build_failed"
    assert trial.finished_at is not None
    assert "Image build for im-abc123 failed" in trial.error_message


def test_store_trial_results_persists_total_steps(monkeypatch):
    trial = SimpleNamespace(
        task_id="task-1",
        status=trial_handler.TrialStatus.RUNNING,
        attempts=1,
        max_attempts=1,
        error_message=None,
        harbor_stage="running",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
    )

    class _Session:
        pass

    @asynccontextmanager
    async def _fake_trial_session(trial_id: str, *, allow_missing: bool = False):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
    )

    outcome = harbor_runner.HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=Path("/tmp/result.json"),
        job_dir=Path("/tmp/job"),
        input_tokens=100,
        cache_tokens=25,
        output_tokens=50,
        total_steps=7,
        cost_usd=0.12,
        has_trajectory=True,
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key="tasks/task-1/trials/trial-1/",
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.SUCCESS
    assert trial.input_tokens == 100
    assert trial.cache_tokens == 25
    assert trial.output_tokens == 50
    assert trial.total_steps == 7
    assert trial.cost_usd == 0.12
    assert trial.has_trajectory is True


def test_store_trial_results_overrides_runtime_cancelled_for_image_build(monkeypatch):
    trial = SimpleNamespace(
        task_id="task-1",
        status=trial_handler.TrialStatus.FAILED,
        attempts=1,
        max_attempts=6,
        error_message=(
            "Trial cancelled by the runtime. This is usually caused by a "
            "worker restart or an environment startup failure. Check worker logs."
        ),
        harbor_stage="cancelled",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
    )

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(trial_id: str, *, allow_missing: bool = False):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
    )
    monkeypatch.setattr(
        queue_module, "enqueue_analysis_worker_job", _fake_enqueue_analysis_worker_job
    )

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="Harbor job execution failed: RuntimeError: Image build for im-xyz789 failed",
        exit_code=-1,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.FAILED
    assert trial.harbor_stage == "image_build_failed"
    assert trial.finished_at is not None
    assert "Image build for im-xyz789 failed" in trial.error_message


def test_store_trial_results_preserves_user_cancel_for_image_build(monkeypatch):
    trial = SimpleNamespace(
        task_id="task-1",
        status=trial_handler.TrialStatus.FAILED,
        attempts=1,
        max_attempts=1,
        error_message="Cancelled by user",
        harbor_stage="cancelled",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id=None,
        current_queue_slot=None,
        heartbeat_at=None,
        finished_at=object(),
    )
    original_finished_at = trial.finished_at

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(trial_id: str, *, allow_missing: bool = False):
        yield _Session(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="Harbor job execution failed: RuntimeError: Image build for im-usercancel failed",
        exit_code=-1,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.FAILED
    assert trial.harbor_stage == "cancelled"
    assert trial.error_message == "Cancelled by user"
    assert trial.finished_at is original_finished_at


def test_run_harbor_trial_async_skips_temp_root_preflight_without_task_patch(
    monkeypatch, tmp_path
):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"
    seen: dict[str, bool] = {}

    def _fake_preflight(path: Path, *, include_temp_root: bool, **_: object) -> None:
        assert path == jobs_dir
        seen["include_temp_root"] = include_temp_root
        return None

    class _FakeJob:
        def __init__(self, config):
            self.job_dir = config["jobs_dir"] / "job-1"

        @classmethod
        async def create(cls, config):
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", _fake_preflight
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )
    monkeypatch.setattr(harbor_runner, "_build_agent_config", lambda **kwargs: object())
    monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
    monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_runner, "Job", _FakeJob)
    monkeypatch.setattr(
        harbor_runner,
        "_extract_outcome_from_job_result",
        lambda **kwargs: harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=kwargs["duration_sec"],
            job_result_path=kwargs["job_result_path"],
            job_dir=kwargs["job_dir"],
        ),
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=jobs_dir,
        )
    )

    assert seen["include_temp_root"] is False
    assert outcome.error is None
    assert outcome.job_result_path is not None


def test_run_harbor_trial_async_probe_skips_timeout_validation(monkeypatch, tmp_path):
    """A probe (mode=probe) against a task whose task.toml omits timeouts must
    run instead of hard-failing — mirroring the local runner, which skips
    validation and applies a capped default agent timeout. Regression guard for
    the cloud/local asymmetry that broke probes in prod."""
    task_path = tmp_path / "task"
    task_path.mkdir()
    # Empty task.toml: declares NO agent/verifier/build timeouts, so the real
    # validator (left unmocked here on purpose) would raise for a non-probe.
    (task_path / "task.toml").write_text("", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"
    captured: dict[str, object] = {}

    class _FakeJob:
        def __init__(self, config):
            captured["config"] = config
            self.job_dir = config["jobs_dir"] / "job-1"

        @classmethod
        async def create(cls, config):
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", lambda *a, **k: None
    )
    monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
    monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_runner, "Job", _FakeJob)
    monkeypatch.setattr(
        harbor_runner,
        "_extract_outcome_from_job_result",
        lambda **kwargs: harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=kwargs["duration_sec"],
            job_result_path=kwargs["job_result_path"],
            job_dir=kwargs["job_dir"],
        ),
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="claude-code",
            jobs_dir=jobs_dir,
            harbor_config={"mode": "probe", "extra_instructions": "look around"},
        )
    )

    assert outcome.error is None
    agent_config = captured["config"]["agents"][0]
    assert agent_config.override_timeout_sec == harbor_runner.PROBE_AGENT_TIMEOUT_SEC


def test_run_harbor_trial_async_non_probe_still_validates(tmp_path):
    """Non-probe trials keep the strict contract: a task.toml without timeouts
    must still raise. The 'skip validation' relaxation is probe-only."""
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")

    with pytest.raises(TaskTimeoutValidationError):
        asyncio.run(
            harbor_runner.run_harbor_trial_async(
                task_path=task_path,
                agent="nop",
                jobs_dir=tmp_path / "jobs",
            )
        )


def test_build_agent_config_injects_probe_timeout_default(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model=None,
        raw_harbor_config={},
        is_probe=True,
    )

    assert agent_config.override_timeout_sec == harbor_runner.PROBE_AGENT_TIMEOUT_SEC


def test_build_agent_config_probe_respects_explicit_override(monkeypatch):
    """An explicit per-trial override must win over the probe default."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model=None,
        raw_harbor_config={"agent_config": {"override_timeout_sec": 42}},
        is_probe=True,
    )

    assert agent_config.override_timeout_sec == 42


def test_build_agent_config_non_probe_leaves_timeout_unset(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model=None,
        raw_harbor_config={},
        is_probe=False,
    )

    assert agent_config.override_timeout_sec is None


def test_build_agent_config_claude_uses_bedrock_id_in_bedrock_mode(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    # No direct-API key -> force-direct is a no-op, so this exercises Bedrock.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "global.anthropic.claude-sonnet-4-6"


def test_build_agent_config_claude_uses_anthropic_api_id_without_bedrock_env(
    monkeypatch,
):
    """Without Bedrock env, Harbor's claude-code agent authenticates against the
    direct Anthropic API. The model id must follow that transport: a Bedrock
    inference-profile id sent to the direct API is rejected with HTTP 400
    "Operation not allowed" (the observed probe-agent crash)."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    # Trial rows persist the already-canonicalized Bedrock id; it must map back
    # to the plain Anthropic API id when the agent runs off ANTHROPIC_API_KEY.
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="global.anthropic.claude-sonnet-4-6",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "claude-sonnet-4-6"


def test_build_agent_config_probe_claude_code_forces_direct_api(monkeypatch):
    """A probe's claude-code agent can't authenticate to Bedrock in its Daytona
    DinD sandbox; with an ANTHROPIC_API_KEY available it must use the direct
    Anthropic API and a matching plain model id (a Bedrock inference-profile id
    over the direct transport 400s with "Operation not allowed")."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="global.anthropic.claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=True,
    )

    assert agent_config.model_name == "claude-sonnet-4-6"


def test_build_agent_config_non_probe_claude_code_keeps_bedrock_id(monkeypatch):
    """With the global force-direct flag OFF, routing is probe-scoped: a normal
    (non-probe) claude-code trial with Bedrock env keeps the Bedrock id even when
    an ANTHROPIC_API_KEY is present."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=False,
    )

    assert agent_config.model_name == "global.anthropic.claude-sonnet-4-6"


def test_build_agent_config_non_probe_forces_direct_api_when_flag_set(monkeypatch):
    """Incident mitigation: with the global force-direct flag ON (default) and an
    ANTHROPIC_API_KEY available, a normal (non-probe) claude-code trial routes to
    the direct Anthropic API -- the Bedrock id maps back to the plain API id."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", True)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="global.anthropic.claude-opus-4-8",
        raw_harbor_config={},
        is_probe=False,
    )

    assert agent_config.model_name == "claude-opus-4-8"


def test_build_agent_config_non_probe_keeps_bedrock_without_anthropic_key(monkeypatch):
    """Force-direct is a no-op without an ANTHROPIC_API_KEY, even with the flag on,
    so the Bedrock route (and id) is preserved for non-key environments."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", True)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=False,
    )

    assert agent_config.model_name == "global.anthropic.claude-sonnet-4-6"


def test_build_agent_config_probe_claude_code_without_anthropic_key_uses_bedrock(
    monkeypatch,
):
    """A probe only forces the direct API when an ANTHROPIC_API_KEY is available;
    absent it, fall back to the Bedrock id (Bedrock routing is unchanged)."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=True,
    )

    assert agent_config.model_name == "global.anthropic.claude-sonnet-4-6"


def test_agent_uses_bedrock_unchanged_by_probe_scoping(monkeypatch):
    """Guard: probe scoping must NOT narrow _agent_uses_bedrock(). The baked-in
    CLAUDE_CODE_USE_BEDROCK=1 flag alone still counts as Bedrock for normal
    trials, with or without a bearer token."""
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert harbor_runner._agent_uses_bedrock() is True


def test_build_agent_config_uses_azure_deployment_without_secret_env(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "azure")
    monkeypatch.setattr(harbor_runner.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_deployments",
        {"openai/gpt-5.4": "oddish-gpt"},
    )

    agent_config = harbor_runner._build_agent_config(
        agent="codex",
        model="openai/gpt-5.4",
        raw_harbor_config={},
    )

    assert agent_config.name is None
    assert agent_config.import_path == (
        "oddish.workers.codex_agent:AzureCompatibleCodex"
    )
    assert agent_config.model_name == "oddish-gpt"
    assert "AZURE_OPENAI_API_KEY" not in agent_config.env
    assert "OPENAI_API_KEY" not in agent_config.env


def test_build_agent_config_uses_oddish_codex_wrapper_for_public_openai(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "openai_api_key", "openai-key")

    agent_config = harbor_runner._build_agent_config(
        agent="codex",
        model="openai/gpt-5.2-codex",
        raw_harbor_config={},
    )

    assert agent_config.name is None
    assert agent_config.import_path == "oddish.workers.codex_agent:OddishCodex"
    assert agent_config.model_name == "openai/gpt-5.2-codex"


def test_build_agent_config_preserves_custom_codex_import(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "openai_api_key", "openai-key")

    agent_config = harbor_runner._build_agent_config(
        agent="codex",
        model="openai/gpt-5.2-codex",
        raw_harbor_config={
            "agent_config": {
                "name": "codex",
                "import_path": "custom.module:CustomCodex",
            }
        },
    )

    assert agent_config.name == "codex"
    assert agent_config.import_path == "custom.module:CustomCodex"


def test_build_agent_config_does_not_wrap_non_codex_agents(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="nop",
        model=None,
        raw_harbor_config={},
    )

    assert agent_config.name == "nop"
    assert agent_config.import_path is None


def test_azure_compatible_codex_disables_unified_exec(tmp_path):
    seen: dict[str, str] = {}

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            seen["command"] = command
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = AzureCompatibleCodex(logs_dir=tmp_path, model_name="oddish-gpt")

    asyncio.run(
        agent.exec_as_agent(
            _FakeEnvironment(),
            "codex exec --json --enable unified_exec -- 'fix it'",
        )
    )

    assert "--disable unified_exec" in seen["command"]
    assert "--enable unified_exec" not in seen["command"]
    assert "-c model_provider='\"oddish_azure_openai\"'" in seen["command"]
    assert "model_verbosity" not in seen["command"]


def test_oddish_codex_retries_server_supported_verbosity(tmp_path):
    seen: list[str] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            seen.append(command)
            if len(seen) == 1:
                return SimpleNamespace(
                    return_code=1,
                    stdout=(
                        '{"type":"error","message":"{\\n'
                        '  \\"error\\": {\\n'
                        '    \\"message\\": \\"Unsupported value: low. '
                        "Supported values are: 'server-selected'.\\\",\\n"
                        '    \\"param\\": \\"text.verbosity\\"\\n'
                        "  }\\n"
                        '}"}'
                    ),
                    stderr="",
                )
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = OddishCodex(logs_dir=tmp_path, model_name="oddish-gpt")

    asyncio.run(
        agent.exec_as_agent(
            _FakeEnvironment(),
            "codex exec --json -- 'fix it'",
        )
    )

    assert len(seen) == 2
    assert "model_verbosity" not in seen[0]
    assert "-c model_verbosity='\"server-selected\"'" in seen[1]


def test_oddish_codex_replaces_explicit_unsupported_verbosity(tmp_path):
    seen: list[str] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            seen.append(command)
            if len(seen) == 1:
                return SimpleNamespace(
                    return_code=1,
                    stdout=(
                        '{"type":"error","message":"{'
                        '\\"error\\": {'
                        '\\"message\\": \\"Unsupported value. '
                        "Supported values are: 'medium'.\\\","
                        '\\"param\\": \\"text.verbosity\\"'
                        '}}"}'
                    ),
                    stderr="",
                )
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    agent = OddishCodex(logs_dir=tmp_path, model_name="oddish-gpt")

    asyncio.run(
        agent.exec_as_agent(
            _FakeEnvironment(),
            "codex exec -c model_verbosity='\"low\"' --json -- 'fix it'",
        )
    )

    assert len(seen) == 2
    assert "-c model_verbosity='\"low\"'" in seen[0]
    assert "-c model_verbosity='\"medium\"'" in seen[1]
    assert seen[1].count("model_verbosity=") == 1


def test_azure_compatible_codex_configures_http_responses_provider(
    monkeypatch, tmp_path
):
    seen: dict[str, str] = {}

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            seen["command"] = command
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    monkeypatch.setenv(
        "OPENAI_BASE_URL",
        "https://example.openai.azure.com/openai/v1",
    )
    # Codex uses the OpenAI-compatible /openai/v1 route here. Do not forward
    # Azure SDK-style api-version values into that route.
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "unsupported-test-version")
    agent = AzureCompatibleCodex(logs_dir=tmp_path, model_name="oddish-gpt")

    asyncio.run(
        agent.exec_as_agent(
            _FakeEnvironment(),
            'cat >>"$CODEX_HOME/config.toml" <<TOML\n'
            'openai_base_url = "${OPENAI_BASE_URL}"\n'
            "TOML\n",
        )
    )

    assert 'model_provider = "oddish_azure_openai"' in seen["command"]
    assert "[model_providers.oddish_azure_openai]" in seen["command"]
    assert 'base_url = "https://example.openai.azure.com/openai/v1"' in seen["command"]
    assert 'wire_api = "responses"' in seen["command"]
    assert "supports_websockets = false" in seen["command"]
    assert "query_params" not in seen["command"]
    assert "api-version" not in seen["command"]
    assert "unsupported-test-version" not in seen["command"]


def test_oddish_codex_writes_stdout_trajectory_when_richer(tmp_path):
    (tmp_path / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.5",
                "agent": {"name": "codex", "version": "0.137.0"},
                "steps": [
                    {"step_id": 1, "source": "system", "message": "setup"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "codex.txt").write_text(
        "\n".join(
            [
                "Reading additional input from stdin...",
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": "thread-1",
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "command": "/bin/bash -lc ls",
                            "aggregated_output": "README.md\n",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_2",
                            "type": "reasoning",
                            "text": "I found the active ticket.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_3",
                            "type": "agent_message",
                            "text": "Done.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 3,
                            "output_tokens": 4,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 14,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    context = SimpleNamespace(
        cost_usd=None,
        n_input_tokens=0,
        n_cache_tokens=0,
        n_output_tokens=0,
    )
    agent = OddishCodex(logs_dir=tmp_path, model_name="gpt-5.2-codex")

    agent.populate_context_post_run(context)

    trajectory = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert trajectory["session_id"] == "thread-1"
    assert trajectory["agent"]["extra"]["trajectory_source"] == "codex_stdout_jsonl"
    assert len(trajectory["steps"]) == 3
    command_step = trajectory["steps"][0]
    assert command_step["tool_calls"][0]["function_name"] == "shell"
    assert command_step["tool_calls"][0]["arguments"]["command"] == "/bin/bash -lc ls"
    assert command_step["observation"]["results"][0]["content"] == "README.md\n"
    assert trajectory["steps"][1]["reasoning_content"] == "I found the active ticket."
    assert trajectory["steps"][2]["message"] == "Done."
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 10
    assert trajectory["final_metrics"]["total_completion_tokens"] == 4
    assert context.n_input_tokens == 10
    assert context.n_cache_tokens == 3
    assert context.n_output_tokens == 4


def test_oddish_codex_keeps_existing_richer_trajectory(tmp_path):
    existing_steps = [
        {"step_id": index + 1, "source": "agent", "message": f"step {index}"}
        for index in range(5)
    ]
    (tmp_path / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.5",
                "agent": {"name": "codex", "version": "0.137.0"},
                "steps": existing_steps,
            }
        ),
        encoding="utf-8",
    )
    original = (tmp_path / "trajectory.json").read_text(encoding="utf-8")
    (tmp_path / "codex.txt").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "short",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    context = SimpleNamespace()
    agent = OddishCodex(logs_dir=tmp_path, model_name="gpt-5.2-codex")

    agent.populate_context_post_run(context)

    assert (tmp_path / "trajectory.json").read_text(encoding="utf-8") == original


def test_trial_uses_openai_provider_before_azure_model_rewrite(monkeypatch):
    assert harbor_runner._trial_uses_openai_provider(
        agent="custom-agent",
        model=None,
        raw_harbor_config={
            "agent_config": {
                "name": "custom-agent",
                "model_name": "openai/gpt-5.4",
            }
        },
    )


def test_run_harbor_trial_async_scopes_azure_env(monkeypatch, tmp_path):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    monkeypatch.delenv("ODDISH_AZURE_OPENAI_DEPLOYMENTS", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "azure")
    monkeypatch.setattr(harbor_runner.settings, "azure_openai_api_key", "az-key")
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_endpoint",
        "https://example.openai.azure.com",
    )
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_api_version",
        "2025-01-01-preview",
    )
    monkeypatch.setattr(
        harbor_runner.settings,
        "azure_openai_deployments",
        {"openai/gpt-5.4": "oddish-gpt"},
    )
    seen: dict[str, str | None] = {}

    class _FakeJob:
        def __init__(self, config):
            self.job_dir = config["jobs_dir"] / "job-1"

        @classmethod
        async def create(cls, config):
            seen["api_key"] = os.environ.get("AZURE_OPENAI_API_KEY")
            seen["endpoint"] = os.environ.get("AZURE_OPENAI_ENDPOINT")
            seen["deployment"] = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
            seen["openai_key"] = os.environ.get("OPENAI_API_KEY")
            seen["base_url"] = os.environ.get("OPENAI_BASE_URL")
            return cls(config)

        async def run(self):
            self.job_dir.mkdir(parents=True, exist_ok=True)
            (self.job_dir / "result.json").write_text("{}\n", encoding="utf-8")
            return object()

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )
    monkeypatch.setattr(harbor_runner, "TaskConfig", lambda path: path)
    monkeypatch.setattr(harbor_runner, "JobConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harbor_runner, "Job", _FakeJob)
    monkeypatch.setattr(
        harbor_runner,
        "_extract_outcome_from_job_result",
        lambda **kwargs: harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=kwargs["duration_sec"],
            job_result_path=kwargs["job_result_path"],
            job_dir=kwargs["job_dir"],
        ),
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="codex",
            jobs_dir=jobs_dir,
            model="openai/gpt-5.4",
        )
    )

    assert outcome.error is None
    assert seen == {
        "api_key": "az-key",
        "endpoint": "https://example.openai.azure.com",
        "deployment": "oddish-gpt",
        "openai_key": "az-key",
        "base_url": "https://example.openai.azure.com/openai/v1",
    }
    assert os.environ.get("AZURE_OPENAI_API_KEY") is None
    assert os.environ.get("OPENAI_API_KEY") is None
    assert os.environ.get("OPENAI_BASE_URL") is None


def test_run_harbor_trial_async_checks_temp_root_when_task_patch_needed(
    monkeypatch, tmp_path
):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    calls: list[bool] = []

    def _fake_preflight(
        path: Path, *, include_temp_root: bool, **_: object
    ) -> str | None:
        calls.append(include_temp_root)
        return "temp root unavailable" if include_temp_root else None

    monkeypatch.setattr(
        harbor_runner, "_check_local_storage_preflight", _fake_preflight
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            harbor_config={"docker_image": "ghcr.io/example/image:latest"},
        )
    )

    assert calls == [True]
    assert outcome.error == "temp root unavailable"
    assert outcome.job_dir is None


def test_cleanup_uploaded_job_dir_prunes_empty_parent(monkeypatch, tmp_path):
    base_dir = tmp_path / "harbor"
    job_dir = base_dir / "task-demo.nop.trial-demo" / "20260422-000000"
    job_dir.mkdir(parents=True)
    (job_dir / "result.json").write_text("{}\n")

    monkeypatch.setattr(trial_handler.settings, "harbor_jobs_dir", str(base_dir))

    trial_handler._cleanup_uploaded_job_dir(job_dir, "trial-demo")

    assert base_dir.exists()
    assert not job_dir.exists()
    assert not job_dir.parent.exists()


def test_cleanup_trial_wrapper_dirs_removes_leaked_wrappers(monkeypatch, tmp_path):
    """Harbor wrapper dirs left behind by failure paths are swept."""
    base_dir = tmp_path / "harbor"
    trial_id = "trial-leak"
    wrapper_a = base_dir / f"task-a.nop.{trial_id}"
    wrapper_b = base_dir / f"task-b.claude-code.{trial_id}"
    unrelated = base_dir / "task-c.nop.other-trial"
    for d in (wrapper_a, wrapper_b, unrelated):
        (d / "some-timestamp").mkdir(parents=True)
        (d / "some-timestamp" / "result.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(trial_handler.settings, "harbor_jobs_dir", str(base_dir))

    trial_handler._cleanup_trial_wrapper_dirs(trial_id)

    assert base_dir.exists()
    assert not wrapper_a.exists()
    assert not wrapper_b.exists()
    assert unrelated.exists()


def test_cleanup_trial_wrapper_dirs_is_noop_when_empty(monkeypatch, tmp_path):
    base_dir = tmp_path / "harbor"
    base_dir.mkdir()
    monkeypatch.setattr(trial_handler.settings, "harbor_jobs_dir", str(base_dir))

    trial_handler._cleanup_trial_wrapper_dirs("trial-missing")

    assert base_dir.exists()


def test_cleanup_trial_wrapper_dirs_skips_missing_base(monkeypatch, tmp_path):
    base_dir = tmp_path / "harbor-does-not-exist"
    monkeypatch.setattr(trial_handler.settings, "harbor_jobs_dir", str(base_dir))

    # Should not raise even though the base directory never existed.
    trial_handler._cleanup_trial_wrapper_dirs("trial-missing")


def _make_retry_decision_trial(*, attempts: int = 1, max_attempts: int = 6):
    return SimpleNamespace(
        task_id="task-retry-gate",
        status=trial_handler.TrialStatus.RUNNING,
        attempts=attempts,
        max_attempts=max_attempts,
        error_message=None,
        harbor_stage="agent",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
        finished_at=None,
    )


def _install_retry_decision_session_fakes(monkeypatch, trial):
    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(trial_id: str, *, allow_missing: bool = False):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
    )
    monkeypatch.setattr(
        queue_module, "enqueue_analysis_worker_job", _fake_enqueue_analysis_worker_job
    )


def test_store_trial_results_skips_retry_for_non_retryable_exception(monkeypatch):
    """A dying-sandbox AddTestsDirError must NOT re-queue the trial: the
    sandbox is gone and a fresh attempt would just hit the same wall after
    burning another full agent timeout. Source of truth for the
    "non-retryable" set is harbor.models.job.config.RetryConfig."""

    trial = _make_retry_decision_trial(attempts=1, max_attempts=6)
    _install_retry_decision_session_fakes(monkeypatch, trial)

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="AddTestsDirError: Failed to add tests directory to environment.",
        exit_code=-1,
        duration_sec=120.0,
        job_result_path=None,
        job_dir=None,
        exception_type="AddTestsDirError",
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.FAILED
    assert trial.finished_at is not None
    # attempts must NOT have been bumped — this is a permanent failure on
    # the first attempt.
    assert trial.attempts == 1


def test_store_trial_results_still_retries_unknown_exception(monkeypatch):
    """Exception types we don't explicitly mark as terminal still go through
    the existing attempts < max_attempts retry path."""

    trial = _make_retry_decision_trial(attempts=1, max_attempts=6)
    _install_retry_decision_session_fakes(monkeypatch, trial)

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="ConnectionResetError: connection reset by peer",
        exit_code=-1,
        duration_sec=5.0,
        job_result_path=None,
        job_dir=None,
        exception_type="ConnectionResetError",
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.RETRYING
    assert trial.finished_at is None


def test_store_trial_results_retries_when_exception_type_is_missing(monkeypatch):
    """Pre-fix HarborOutcome rows have exception_type=None; retry behavior
    for those must match the previous default (re-queue while attempts
    remain) — we only short-circuit when we positively identify the
    failure as terminal."""

    trial = _make_retry_decision_trial(attempts=1, max_attempts=6)
    _install_retry_decision_session_fakes(monkeypatch, trial)

    outcome = harbor_runner.HarborOutcome(
        reward=None,
        error="some generic harness error with no exception_type",
        exit_code=-1,
        duration_sec=5.0,
        job_result_path=None,
        job_dir=None,
        exception_type=None,
    )

    asyncio.run(
        trial_handler._store_trial_results(
            trial_id="trial-1",
            outcome=outcome,
            trial_s3_key=None,
            execution_error=None,
        )
    )

    assert trial.status == trial_handler.TrialStatus.RETRYING


def test_non_retryable_set_includes_known_terminal_failures():
    """Tripwire: if Harbor's RetryConfig defaults change, we want the test
    to fail loudly so we can decide whether to track the new entry."""

    expected = {
        "AddTestsDirError",
        "AgentTimeoutError",
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
    }
    assert expected <= trial_handler._NON_RETRYABLE_EXCEPTION_TYPES


def test_extract_outcome_from_job_result_carries_exception_type(monkeypatch):
    """``HarborOutcome.exception_type`` must be sourced from
    ``TrialResult.exception_info.exception_type`` so the retry gate can
    consult it."""

    trial_result = SimpleNamespace(
        exception_info=SimpleNamespace(
            exception_type="AddTestsDirError",
            exception_message="Failed to add tests directory to environment.",
        ),
        agent_result=None,
        verifier_result=None,
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    job_result = SimpleNamespace(
        trial_results=[trial_result],
        stats=SimpleNamespace(evals={}),
    )

    outcome = harbor_runner._extract_outcome_from_job_result(
        job_result=job_result,
        job_result_path=Path("/tmp/result.json"),
        job_dir=Path("/tmp"),
        duration_sec=1.0,
    )

    assert outcome.exception_type == "AddTestsDirError"
    assert outcome.error and "Failed to add tests directory" in outcome.error


def test_extract_outcome_from_job_result_reads_trajectory_steps(tmp_path):
    traj_dir = tmp_path / "trial" / "agent"
    traj_dir.mkdir(parents=True)
    (traj_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "final_metrics": {
                    "total_prompt_tokens": 11,
                    "total_completion_tokens": 7,
                    "total_cached_tokens": 3,
                    "total_steps": 5,
                    "total_cost_usd": 0.42,
                },
                "steps": [{"step_id": index} for index in range(99)],
            }
        ),
        encoding="utf-8",
    )
    trial_result = SimpleNamespace(
        exception_info=None,
        agent_result=None,
        verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    job_result = SimpleNamespace(
        trial_results=[trial_result],
        stats=SimpleNamespace(evals={}),
    )

    outcome = harbor_runner._extract_outcome_from_job_result(
        job_result=job_result,
        job_result_path=tmp_path / "result.json",
        job_dir=tmp_path,
        duration_sec=1.0,
    )

    assert outcome.input_tokens == 11
    assert outcome.output_tokens == 7
    assert outcome.cache_tokens == 3
    assert outcome.total_steps == 5
    assert outcome.cost_usd == 0.42
    assert outcome.has_trajectory is True


def test_extract_outcome_from_job_result_counts_steps_when_agent_context_exists(tmp_path):
    traj_dir = tmp_path / "trial" / "agent"
    traj_dir.mkdir(parents=True)
    (traj_dir / "trajectory.json").write_text(
        json.dumps({"steps": [{"step_id": "a"}, {"step_id": "b"}]}),
        encoding="utf-8",
    )
    agent_context = SimpleNamespace(
        is_empty=lambda: False,
        n_input_tokens=10,
        n_cache_tokens=4,
        n_output_tokens=6,
        cost_usd=None,
    )
    trial_result = SimpleNamespace(
        exception_info=None,
        agent_result=agent_context,
        verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    job_result = SimpleNamespace(
        trial_results=[trial_result],
        stats=SimpleNamespace(evals={}),
    )

    outcome = harbor_runner._extract_outcome_from_job_result(
        job_result=job_result,
        job_result_path=tmp_path / "result.json",
        job_dir=tmp_path,
        duration_sec=1.0,
    )

    assert outcome.input_tokens == 10
    assert outcome.output_tokens == 6
    assert outcome.total_steps == 2


def test_extract_outcome_from_job_result_exception_type_none_when_no_exc():
    """A successful trial (no exception_info) must leave exception_type as
    None so we don't accidentally surface a placeholder string into retry
    logic."""

    trial_result = SimpleNamespace(
        exception_info=None,
        agent_result=None,
        verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    job_result = SimpleNamespace(
        trial_results=[trial_result],
        stats=SimpleNamespace(evals={}),
    )

    outcome = harbor_runner._extract_outcome_from_job_result(
        job_result=job_result,
        job_result_path=Path("/tmp/result.json"),
        job_dir=Path("/tmp"),
        duration_sec=1.0,
    )

    assert outcome.exception_type is None
    assert outcome.reward == 1.0
