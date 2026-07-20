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
from harbor.models.environment_type import EnvironmentType  # noqa: E402
from harbor.models.trial.config import (  # noqa: E402
    AgentConfig as HarborAgentConfig,
    EnvironmentConfig as HarborEnvironmentConfig,
)

from oddish.task_timeouts import TaskTimeoutValidationError  # noqa: E402
from oddish.workers.agents.codex import AzureCompatibleCodex, OddishCodex  # noqa: E402
from oddish.workers.agents.grok_build import OddishGrokBuild  # noqa: E402
from oddish.workers.agents.grok_build_trajectory import (  # noqa: E402
    convert_grok_build_json_text_to_trajectory,
)
from oddish.workers.harbor import runner as harbor_runner  # noqa: E402
from oddish.workers.harbor import agent_config as harbor_agent_config  # noqa: E402
from oddish.workers.harbor import storage as harbor_storage  # noqa: E402
from oddish.workers.queue import trial_handler  # noqa: E402

_DISK_USAGE = namedtuple("DiskUsage", ["total", "used", "free"])


def _write_network_policy_task(
    tmp_path: Path,
    *,
    environment_mode: str = "public",
    agent_mode: str = "no-network",
    compose: bool = False,
) -> Path:
    task_path = tmp_path / "task"
    environment_dir = task_path / "environment"
    environment_dir.mkdir(parents=True)
    (task_path / "task.toml").write_text(
        f"""schema_version = "1.3"

[environment]
network_mode = "{environment_mode}"

[agent]
network_mode = "{agent_mode}"
""",
        encoding="utf-8",
    )
    if compose:
        (environment_dir / "docker-compose.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )
    return task_path


def test_inject_daytona_agent_model_hosts_for_restricted_direct_task(
    monkeypatch, tmp_path
):
    task_path = _write_network_policy_task(tmp_path)
    environment_config = HarborEnvironmentConfig(type=EnvironmentType.DAYTONA)
    agent_config = HarborAgentConfig(
        import_path="example.agent:Agent",
        model_name="example-model",
        env={"MODEL_BASE_URL": "https://model.test/v1"},
        extra_allowed_hosts=["existing.test"],
    )
    captured: dict[str, object] = {}

    def _infer(**kwargs):
        captured.update(kwargs)
        return ["model.test", "existing.test"]

    monkeypatch.setattr(harbor_runner, "infer_agent_domains", _infer)

    harbor_runner._inject_daytona_agent_model_hosts(
        task_path=task_path,
        environment_config=environment_config,
        agent_config=agent_config,
    )

    assert agent_config.extra_allowed_hosts == ["existing.test", "model.test"]
    assert captured["agent_kwargs"] == {
        "extra_env": {"MODEL_BASE_URL": "https://model.test/v1"}
    }


@pytest.mark.parametrize(
    ("environment_type", "environment_mode", "agent_mode", "compose"),
    [
        (EnvironmentType.MODAL, "public", "no-network", False),
        (EnvironmentType.DAYTONA, "public", "public", False),
        (EnvironmentType.DAYTONA, "no-network", "no-network", False),
        (EnvironmentType.DAYTONA, "public", "no-network", True),
    ],
)
def test_inject_daytona_agent_model_hosts_skips_unsupported_shapes(
    monkeypatch,
    tmp_path,
    environment_type,
    environment_mode,
    agent_mode,
    compose,
):
    task_path = _write_network_policy_task(
        tmp_path,
        environment_mode=environment_mode,
        agent_mode=agent_mode,
        compose=compose,
    )
    environment_config = HarborEnvironmentConfig(type=environment_type)
    agent_config = HarborAgentConfig(name="codex", model_name="example-model")
    infer_calls = 0

    def _infer(**kwargs):
        nonlocal infer_calls
        infer_calls += 1
        return ["model.test"]

    monkeypatch.setattr(harbor_runner, "infer_agent_domains", _infer)

    harbor_runner._inject_daytona_agent_model_hosts(
        task_path=task_path,
        environment_config=environment_config,
        agent_config=agent_config,
    )

    assert infer_calls == 0
    assert agent_config.extra_allowed_hosts == []


def test_check_local_storage_preflight_reports_low_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harbor_storage.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_storage.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=9, free=1),
    )
    monkeypatch.setattr(
        harbor_storage.os,
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
        harbor_storage.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_storage.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_storage.os,
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
        harbor_storage.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_storage.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_storage.os,
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
        harbor_storage.tempfile, "gettempdir", lambda: str(tmp_path / "tmp")
    )
    monkeypatch.setattr(
        harbor_storage.shutil,
        "disk_usage",
        lambda path: _DISK_USAGE(total=10, used=1, free=6 * 1024**3),
    )
    monkeypatch.setattr(
        harbor_storage.os,
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

    monkeypatch.setattr(harbor_storage.tempfile, "gettempdir", lambda: str(temp_root))
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
        model="gpt-5",
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(
        trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
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
        model="gpt-5",
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    class _Session:
        pass

    @asynccontextmanager
    async def _fake_trial_session(
        trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_maybe_gate_llm_trials(session, trial_id: str) -> bool:
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
    )
    monkeypatch.setattr(
        queue_module, "maybe_gate_llm_trials", _fake_maybe_gate_llm_trials
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
        model="gpt-5",
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(
        trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
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
        model="gpt-5",
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )
    original_finished_at = trial.finished_at

    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(
        trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
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
    assert (
        agent_config.override_timeout_sec == harbor_agent_config.PROBE_AGENT_TIMEOUT_SEC
    )


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

    assert (
        agent_config.override_timeout_sec == harbor_agent_config.PROBE_AGENT_TIMEOUT_SEC
    )


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


def test_build_agent_config_wraps_non_probe_claude_code_for_stdin_prompt(
    monkeypatch,
):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model=None,
        raw_harbor_config={},
        is_probe=False,
    )

    assert agent_config.name is None
    assert agent_config.import_path == (
        "oddish.workers.agents.claude_code:OddishClaudeCode"
    )


def test_build_agent_config_uses_probe_claude_code_wrapper(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model=None,
        raw_harbor_config={},
        is_probe=True,
    )

    assert agent_config.name is None
    assert agent_config.import_path == (
        "oddish.workers.agents.claude_code:OddishProbeClaudeCode"
    )


def test_build_agent_config_mini_swe_anthropic_uses_oddish_wrapper(monkeypatch):
    """Non-Meta mini-swe-agent trials route through the Oddish base subclass so
    install() pulls litellm's proxy extras (orjson/fastapi) into the tool venv."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent",
        model="claude-opus-4-8",
        raw_harbor_config={},
    )

    assert (
        agent_config.import_path
        == "oddish.workers.agents.mini_swe_agent:OddishMiniSweAgent"
    )


def test_build_agent_config_mini_swe_meta_uses_meta_wrapper(monkeypatch):
    """Meta-model mini-swe-agent trials still route through the Meta subclass."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent",
        model="meta/llama-eval-model",
        raw_harbor_config={},
    )

    assert (
        agent_config.import_path
        == "oddish.workers.agents.mini_swe_agent:OddishMetaMiniSweAgent"
    )


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


def test_build_agent_config_probe_sets_subagent_model(monkeypatch):
    """A probe claude-code agent pins CLAUDE_CODE_SUBAGENT_MODEL to the same
    normalized model id so Task-tool subagents have a model on the direct-API
    path (Harbor's run() only sets it on the custom-base-url branch)."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="global.anthropic.claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=True,
    )

    assert (agent_config.env or {}).get("CLAUDE_CODE_SUBAGENT_MODEL") == (
        agent_config.model_name
    )
    assert agent_config.model_name == "claude-sonnet-4-6"


def test_build_agent_config_litellm_agent_claude_gets_anthropic_prefix(monkeypatch):
    """A litellm-based agent (mini-swe) needs a "provider/model" id. Even in the
    workers' default Bedrock mode, Claude must be handed as
    "anthropic/<api-id>" — the bare Bedrock inference-profile id claude-code
    consumes has no provider prefix and Harbor's mini-swe rejects it with
    "Model name must be in the format provider/model_name"."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")

    # Trial rows persist the canonicalized Bedrock id.
    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent",
        model="global.anthropic.claude-opus-4-8",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "anthropic/claude-opus-4-8"


def test_build_agent_config_litellm_agent_bare_claude_gets_anthropic_prefix(
    monkeypatch,
):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent",
        model="claude-opus-4-8",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "anthropic/claude-opus-4-8"


def test_build_agent_config_litellm_agent_non_claude_model_unchanged(monkeypatch):
    """The litellm prefix rule only rewrites Claude ids; other providers already
    carry their own prefix and must pass through untouched."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent",
        model="gemini-3-pro",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "gemini-3-pro"


def test_build_agent_config_claude_code_keeps_bare_bedrock_id(monkeypatch):
    """Contrast with the litellm agents: claude-code still gets the bare Bedrock
    inference-profile id for its InvokeModel transport."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-opus-4-8",
        raw_harbor_config={},
    )

    assert agent_config.model_name == "global.anthropic.claude-opus-4-8"


def test_build_agent_config_non_probe_omits_subagent_model(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={},
        is_probe=False,
    )

    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in (agent_config.env or {})


def test_build_agent_config_probe_respects_existing_subagent_model(monkeypatch):
    """A pre-set subagent model (e.g. from a provider env shaper) is never clobbered."""
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="claude-sonnet-4-6",
        raw_harbor_config={
            "agent_config": {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "preset-model"}}
        },
        is_probe=True,
    )

    assert (agent_config.env or {})["CLAUDE_CODE_SUBAGENT_MODEL"] == "preset-model"


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
    assert harbor_agent_config._agent_uses_bedrock() is True


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
        "oddish.workers.agents.codex:AzureCompatibleCodex"
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
    assert agent_config.import_path == "oddish.workers.agents.codex:OddishCodex"
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


def test_build_agent_config_preserves_grok_build_xai_route(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "azure")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-token")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-secret")

    agent_config = harbor_runner._build_agent_config(
        agent="grok-build",
        model="xai/redacted-model",
        raw_harbor_config={},
    )

    assert agent_config.name is None
    assert (
        agent_config.import_path == "oddish.workers.agents.grok_build:OddishGrokBuild"
    )
    assert agent_config.model_name == "xai/redacted-model"
    assert agent_config.kwargs["reasoning_effort"] == "high"
    assert "XAI_API_KEY" not in (agent_config.env or {})
    assert "ANTHROPIC_AUTH_TOKEN" not in (agent_config.env or {})
    assert "OPENAI_API_KEY" not in (agent_config.env or {})


def test_build_agent_config_canonicalizes_grok_prefix_to_xai(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")

    agent_config = harbor_runner._build_agent_config(
        agent="grok-build",
        model="grok/redacted-model",
        raw_harbor_config={},
    )

    assert agent_config.name is None
    assert (
        agent_config.import_path == "oddish.workers.agents.grok_build:OddishGrokBuild"
    )
    assert agent_config.model_name == "xai/redacted-model"


def test_build_agent_config_preserves_grok_build_reasoning_override():
    agent_config = harbor_runner._build_agent_config(
        agent="grok-build",
        model="xai/redacted-model",
        raw_harbor_config={"agent_config": {"kwargs": {"reasoning_effort": "medium"}}},
    )

    assert agent_config.kwargs["reasoning_effort"] == "medium"


def test_convert_grok_build_stream_to_multi_step_trajectory():
    raw = "\n".join(
        [
            json.dumps({"type": "thought", "data": "First reasoning sentence. "}),
            json.dumps({"type": "thought", "data": "Second reasoning sentence. "}),
            json.dumps({"type": "thought", "data": "x" * 3000}),
            json.dumps({"type": "text", "data": "Implemented the fix. "}),
            json.dumps({"type": "text", "data": "All checks passed."}),
            json.dumps({"type": "end", "sessionId": "session-1"}),
        ]
    )

    trajectory = convert_grok_build_json_text_to_trajectory(
        raw,
        agent_version="grok 0.2.73",
        model_name="xai/redacted-model",
    )

    assert trajectory is not None
    assert trajectory.session_id == "session-1"
    assert trajectory.agent.name == "grok-build"
    assert len(trajectory.steps) >= 3
    assert trajectory.steps[0].reasoning_content
    assert trajectory.steps[-1].message == "Implemented the fix. All checks passed."
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_steps == len(trajectory.steps)


def test_oddish_grok_build_requests_streaming_json(tmp_path):
    seen: list[str] = []

    uploads: list[str] = []

    class _FakeEnvironment:
        async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
            seen.append(command)
            return SimpleNamespace(return_code=0, stdout="", stderr="")

        async def upload_file(self, source_path, target_path):
            uploads.append(target_path)

    agent = OddishGrokBuild(logs_dir=tmp_path, model_name="xai/redacted-model")

    asyncio.run(agent.run("fix it", _FakeEnvironment(), SimpleNamespace()))

    run_command = next(c for c in seen if "grok -p" in c)
    # The session store is captured out-of-band after the grok run.
    assert any("grok-session" in c for c in seen)
    assert "--output-format streaming-json" in run_command
    assert "--output-format json" in run_command
    assert "--reasoning-effort high" in run_command
    assert "reasoning-effort|reasoning_effort" in run_command
    assert "streaming-json|output-format|no-auto-update" in run_command
    assert ">/logs/agent/grok-build.json" in run_command
    # The instruction is staged out-of-band and read back inside the sandbox,
    # never inlined into the exec argv (Modal ARG_MAX guard).
    assert uploads == ["/tmp/oddish-grok-build-prompt.txt"]
    assert 'grok -p "$(cat /tmp/oddish-grok-build-prompt.txt)"' in run_command
    assert "fix it" not in run_command


def test_oddish_grok_build_writes_streaming_json_trajectory(tmp_path):
    (tmp_path / "grok-build.json").write_text(
        "\n".join(
            [
                json.dumps({"type": "reasoning", "text": "Need to inspect files."}),
                json.dumps(
                    {
                        "type": "tool_call",
                        "id": "call_1",
                        "name": "shell",
                        "arguments": {"command": "ls"},
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "tool_call_id": "call_1",
                        "output": "README.md\n",
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "Done.",
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
    agent = OddishGrokBuild(logs_dir=tmp_path, model_name="xai/redacted-model")

    agent.populate_context_post_run(context)

    trajectory = json.loads((tmp_path / "trajectory.json").read_text(encoding="utf-8"))
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["agent"]["name"] == "grok-build"
    assert len(trajectory["steps"]) == 3
    assert trajectory["steps"][0]["reasoning_content"] == "Need to inspect files."
    assert trajectory["steps"][0]["tool_calls"][0]["function_name"] == "shell"
    assert (
        trajectory["steps"][1]["observation"]["results"][0]["content"] == "README.md\n"
    )
    assert trajectory["steps"][1]["extra"]["source_call_id"] == "call_1"
    assert trajectory["steps"][2]["message"] == "Done."
    assert trajectory["final_metrics"]["total_steps"] == 3


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


def _byok_runner_doubles(monkeypatch, seen):
    class _FakeJob:
        def __init__(self, config):
            seen["model_name"] = config["agents"][0].model_name
            seen["ambient_anthropic"] = os.environ.get("ANTHROPIC_API_KEY")
            seen["ambient_bedrock"] = os.environ.get("CLAUDE_CODE_USE_BEDROCK")
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


def test_run_harbor_trial_async_byok_forces_direct_without_platform_key(
    monkeypatch, tmp_path
):
    """A BYOK user key routes claude-code to the direct Anthropic API even when
    the worker has no platform key. The routing decision reads os.environ, so
    the user key is surfaced as ambient: the model id follows the direct
    transport and the baked-in Bedrock creds are blanked, and the agent
    authenticates with the user's key."""
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    # No platform Anthropic key, but the image bakes in Bedrock.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer")
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", True)
    seen: dict[str, object] = {}
    _byok_runner_doubles(monkeypatch, seen)

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="claude-code",
            jobs_dir=tmp_path / "jobs",
            model="global.anthropic.claude-sonnet-4-6",
            extra_agent_env={"ANTHROPIC_API_KEY": "sk-user-byok"},
        )
    )

    assert outcome.error is None
    # Routed to the direct Anthropic id, not the Bedrock inference-profile id.
    assert seen["model_name"] == "claude-sonnet-4-6"
    # The user key was ambient at Job.create and Bedrock was blanked for the run.
    assert seen["ambient_anthropic"] == "sk-user-byok"
    assert seen["ambient_bedrock"] == ""
    # Nothing leaked past the trial: the temporary env was restored.
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"


def test_run_harbor_trial_async_no_byok_keeps_bedrock_without_platform_key(
    monkeypatch, tmp_path
):
    """Control: with no BYOK key and no platform key, claude-code still routes to
    Bedrock. The fix forces direct only when a user key is actually present."""
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "openai")
    monkeypatch.setattr(harbor_runner.settings, "claude_code_force_direct_api", True)
    seen: dict[str, object] = {}
    _byok_runner_doubles(monkeypatch, seen)

    outcome = asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="claude-code",
            jobs_dir=tmp_path / "jobs",
            model="global.anthropic.claude-sonnet-4-6",
        )
    )

    assert outcome.error is None
    assert seen["model_name"] == "global.anthropic.claude-sonnet-4-6"
    assert seen["ambient_anthropic"] is None
    assert seen["ambient_bedrock"] == "1"


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
        model="gpt-5",
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
        superseded_by_trial_id=None,
        deleted_at=None,
    )


def _install_retry_decision_session_fakes(monkeypatch, trial):
    class _Session:
        async def get(self, model, obj_id):
            return None

    @asynccontextmanager
    async def _fake_trial_session(
        trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield _Session(), trial

    async def _fake_maybe_start_qa_stage(session, trial_id: str) -> bool:
        return False

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_qa_stage", _fake_maybe_start_qa_stage
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


def test_extract_outcome_from_job_result_counts_steps_when_agent_context_exists(
    tmp_path,
):
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


def test_extract_outcome_from_job_result_prefers_later_agent_context_over_trajectory(
    tmp_path,
):
    traj_dir = tmp_path / "first-trial" / "agent"
    traj_dir.mkdir(parents=True)
    (traj_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "final_metrics": {
                    "total_prompt_tokens": 1,
                    "total_completion_tokens": 2,
                    "total_cached_tokens": 3,
                    "total_steps": 4,
                }
            }
        ),
        encoding="utf-8",
    )
    agent_context = SimpleNamespace(
        is_empty=lambda: False,
        n_input_tokens=10,
        n_cache_tokens=4,
        n_output_tokens=6,
        cost_usd=None,
    )
    first_trial = SimpleNamespace(
        exception_info=None,
        agent_result=None,
        verifier_result=None,
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    second_trial = SimpleNamespace(
        exception_info=None,
        agent_result=agent_context,
        verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
    )
    job_result = SimpleNamespace(
        trial_results=[first_trial, second_trial],
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
    assert outcome.cache_tokens == 4
    assert outcome.total_steps == 4


def test_extract_outcome_from_job_result_uses_single_non_reward_metric():
    trial_result = SimpleNamespace(
        exception_info=None,
        agent_result=None,
        verifier_result=SimpleNamespace(rewards={"score": 0.5}),
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

    assert outcome.reward == 0.5


def test_extract_outcome_from_job_result_ignores_non_numeric_reward():
    trial_result = SimpleNamespace(
        exception_info=None,
        agent_result=None,
        verifier_result=SimpleNamespace(rewards={"reward": "not-a-number"}),
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

    assert outcome.reward is None


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


def test_probe_modal_kwargs_injects_cli_content(monkeypatch):
    """Modal + probe: env_config.kwargs gets probe_cli_content + probe_cli_path."""
    monkeypatch.setattr(
        harbor_runner,
        "_read_query_cli_text",
        lambda: "#!/usr/bin/env node\nconsole.log('hello');",
    )
    from harbor.models.environment_type import EnvironmentType

    kwargs = harbor_runner._probe_modal_kwargs(
        is_probe=True, environment=EnvironmentType.MODAL
    )
    assert kwargs["probe_cli_content"].startswith("#!/usr/bin/env node")
    assert kwargs["probe_cli_path"] == "/probe-harness/oddish-query"


def test_probe_modal_kwargs_returns_empty_for_non_probe(monkeypatch):
    """Non-probe: no CLI content injected even on Modal."""
    from harbor.models.environment_type import EnvironmentType

    kwargs = harbor_runner._probe_modal_kwargs(
        is_probe=False, environment=EnvironmentType.MODAL
    )
    assert kwargs == {}


def test_probe_modal_kwargs_returns_empty_for_non_modal_probe(monkeypatch):
    """Probe on Daytona (non-Modal): no CLI content injected."""
    from harbor.models.environment_type import EnvironmentType

    kwargs = harbor_runner._probe_modal_kwargs(
        is_probe=True, environment=EnvironmentType.DAYTONA
    )
    assert kwargs == {}


def test_gke_env_build_multiplier_covers_pod_ready_wait():
    # For a GKE trial the outer environment-build wait (multiplier x base) must
    # clear the pod-ready/capacity wait, or Harbor deletes the Pending Pod early.
    from harbor.models.environment_type import EnvironmentType

    base = harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC
    multiplier = harbor_runner._sized_environment_build_timeout_multiplier(
        environment=EnvironmentType.GKE,
        environment_build_timeout_multiplier=None,
        timeout_multiplier=None,
        pod_ready_timeout_sec=3600,
        base_sec=base,
    )
    assert multiplier is not None
    assert multiplier * base >= 3600


def test_gke_env_build_multiplier_sizes_off_the_task_base():
    # needed is computed against the task's OWN build_timeout_sec, so a task with
    # a sub-default base still gets an outer wait that clears the pod-ready wait.
    from harbor.models.environment_type import EnvironmentType

    base = 120.0
    multiplier = harbor_runner._sized_environment_build_timeout_multiplier(
        environment=EnvironmentType.GKE,
        environment_build_timeout_multiplier=None,
        timeout_multiplier=None,
        pod_ready_timeout_sec=3600,
        base_sec=base,
    )
    assert multiplier is not None
    assert multiplier * base >= 3600 + harbor_runner._GKE_ENV_BUILD_OVERHEAD_SEC


def test_gke_env_build_multiplier_never_lowers_caller_value():
    # A larger caller-supplied env-build multiplier must be preserved, not reduced.
    from harbor.models.environment_type import EnvironmentType

    multiplier = harbor_runner._sized_environment_build_timeout_multiplier(
        environment=EnvironmentType.GKE,
        environment_build_timeout_multiplier=10.0,
        timeout_multiplier=None,
        pod_ready_timeout_sec=3600,
        base_sec=harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC,
    )
    assert multiplier == 10.0


def test_gke_env_build_multiplier_honors_general_timeout_multiplier_floor():
    # Harbor resolves an unset env-build multiplier to the general
    # timeout_multiplier, so a large general multiplier must not be clobbered.
    from harbor.models.environment_type import EnvironmentType

    multiplier = harbor_runner._sized_environment_build_timeout_multiplier(
        environment=EnvironmentType.GKE,
        environment_build_timeout_multiplier=None,
        timeout_multiplier=100.0,
        pod_ready_timeout_sec=3600,
        base_sec=harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC,
    )
    assert multiplier == 100.0


def test_non_gke_env_build_multiplier_is_untouched():
    # Non-GKE environments keep exactly what the caller passed (including None),
    # never sizing off the base even when it is small.
    from harbor.models.environment_type import EnvironmentType

    assert (
        harbor_runner._sized_environment_build_timeout_multiplier(
            environment=EnvironmentType.MODAL,
            environment_build_timeout_multiplier=None,
            timeout_multiplier=None,
            pod_ready_timeout_sec=3600,
            base_sec=120.0,
        )
        is None
    )
    assert (
        harbor_runner._sized_environment_build_timeout_multiplier(
            environment=EnvironmentType.DOCKER,
            environment_build_timeout_multiplier=2.0,
            timeout_multiplier=5.0,
            pod_ready_timeout_sec=3600,
            base_sec=120.0,
        )
        == 2.0
    )


def test_effective_task_build_timeout_reads_task_toml(tmp_path):
    # The sizing base is the task's OWN build_timeout_sec, read from task.toml.
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.task.config import TaskConfig as _TaskCfg

    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text(
        _TaskCfg(
            environment=EnvironmentConfig(build_timeout_sec=120)
        ).model_dump_toml(),
        encoding="utf-8",
    )
    assert harbor_runner._effective_task_build_timeout_sec(task_path) == 120.0


def test_effective_task_build_timeout_falls_back_to_harbor_default(tmp_path):
    # An absent/unreadable task.toml must not crash the sizing arithmetic; fall
    # back to Harbor's default base.
    from harbor.models.task.config import EnvironmentConfig

    task_path = tmp_path / "task"
    task_path.mkdir()  # no task.toml written
    assert (
        harbor_runner._effective_task_build_timeout_sec(task_path)
        == EnvironmentConfig.model_fields["build_timeout_sec"].default
    )


def test_effective_pod_ready_timeout_prefers_kwargs_override():
    # A submission override in the merged env kwargs beats the platform default.
    assert (
        harbor_runner._effective_pod_ready_timeout_sec(
            {"pod_ready_timeout_sec": 7200}, 3600
        )
        == 7200
    )


def test_effective_pod_ready_timeout_coerces_string_override():
    # --environment-kwarg values arrive as strings; coerce before the arithmetic
    # in the sizing helper (which expects an int).
    assert (
        harbor_runner._effective_pod_ready_timeout_sec(
            {"pod_ready_timeout_sec": "7200"}, 3600
        )
        == 7200
    )


def test_effective_pod_ready_timeout_falls_back_when_absent():
    # No override -> the platform default stands.
    assert harbor_runner._effective_pod_ready_timeout_sec({}, 3600) == 3600


def test_effective_pod_ready_timeout_falls_back_when_unparseable():
    # A malformed value must not crash the sizing arithmetic; fall back safely.
    assert (
        harbor_runner._effective_pod_ready_timeout_sec(
            {"pod_ready_timeout_sec": "not-a-number"}, 3600
        )
        == 3600
    )


def test_gke_env_build_multiplier_sizes_off_effective_pod_ready_override(
    monkeypatch, tmp_path
):
    # A submission can raise pod_ready_timeout_sec above the platform default via
    # environment kwargs (caller-wins in harbor_env_kwargs). The outer build wait
    # must be sized to that larger EFFECTIVE value, not the smaller raw setting,
    # or Harbor deletes a still-Pending flex-start Pod before capacity is granted.
    from harbor.models.environment_type import EnvironmentType
    from oddish.runtime.backends.gke import GkeBackend

    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("", encoding="utf-8")
    jobs_dir = tmp_path / "jobs"

    setting = 3600
    override = 7200
    monkeypatch.setattr(harbor_runner.settings, "gke_pod_ready_timeout_sec", setting)
    # GKE registered on the worker: harbor_env_kwargs seeds the default, then the
    # submission override in env kwargs wins -- the merge the runner must honour.
    monkeypatch.setattr(harbor_runner, "get_backend", lambda name: GkeBackend())

    captured: dict = {}

    class _FakeJob:
        def __init__(self, config):
            captured.update(config)
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
            agent="claude-code",
            jobs_dir=jobs_dir,
            model="global.anthropic.claude-sonnet-4-6",
            environment=EnvironmentType.GKE,
            harbor_config={
                "environment": {
                    "type": "gke",
                    "kwargs": {"pod_ready_timeout_sec": override},
                }
            },
        )
    )

    assert outcome.error is None
    multiplier = captured["environment_build_timeout_multiplier"]
    outer_wait = multiplier * harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC
    assert outer_wait >= override + harbor_runner._GKE_ENV_BUILD_OVERHEAD_SEC
    # Strictly larger than sizing off the raw setting alone would have produced --
    # the early-deletion regression this guards against.
    setting_only = harbor_runner._sized_environment_build_timeout_multiplier(
        environment=EnvironmentType.GKE,
        environment_build_timeout_multiplier=None,
        timeout_multiplier=None,
        pod_ready_timeout_sec=setting,
        base_sec=harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC,
    )
    assert multiplier > setting_only


def test_gke_env_build_multiplier_sizes_off_small_task_build_timeout(
    monkeypatch, tmp_path
):
    # A TPU task may set an environment build_timeout_sec BELOW the 600s default.
    # Harbor sizes the outer environment-build wait as multiplier x the TASK's own
    # build_timeout_sec, so the multiplier must be computed against that real base
    # (not a fixed 600) or the outer wait falls short of the pod-ready timeout and
    # the still-Pending flex-start Pod is deleted before capacity is granted.
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.task.config import TaskConfig as _TaskCfg
    from oddish.runtime.backends.gke import GkeBackend

    task_path = tmp_path / "task"
    task_path.mkdir()
    task_base = 120
    (task_path / "task.toml").write_text(
        _TaskCfg(
            environment=EnvironmentConfig(build_timeout_sec=task_base)
        ).model_dump_toml(),
        encoding="utf-8",
    )
    jobs_dir = tmp_path / "jobs"

    pod_ready = 3600
    monkeypatch.setattr(harbor_runner.settings, "gke_pod_ready_timeout_sec", pod_ready)
    monkeypatch.setattr(harbor_runner, "get_backend", lambda name: GkeBackend())

    captured: dict = {}

    class _FakeJob:
        def __init__(self, config):
            captured.update(config)
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
            agent="claude-code",
            jobs_dir=jobs_dir,
            model="global.anthropic.claude-sonnet-4-6",
            environment=EnvironmentType.GKE,
        )
    )

    assert outcome.error is None
    multiplier = captured["environment_build_timeout_multiplier"]
    # Outer wait = multiplier x the task's OWN base must clear pod_ready + overhead.
    assert (
        multiplier * task_base >= pod_ready + harbor_runner._GKE_ENV_BUILD_OVERHEAD_SEC
    )


def test_ephemeral_gke_trial_receives_sized_env_build_multiplier(tmp_path, monkeypatch):
    # The ephemeral (out-of-process) path runs a GKE environment too, so it MUST
    # get the same pod-ready-covering env-build multiplier as the in-process path.
    # The variant early-return used to skip sizing, so a GKE override trial ran
    # with no multiplier and Harbor deleted the still-Pending flex-start Pod.
    from harbor.models.environment_type import EnvironmentType
    import oddish.workers.harbor.ephemeral as harbor_ephemeral

    captured: dict = {}

    async def _fake_ephemeral(**kwargs):
        captured.update(kwargs)
        return harbor_runner.HarborOutcome(
            reward=None,
            error=None,
            exit_code=0,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
        )

    monkeypatch.setattr(
        harbor_ephemeral, "run_ephemeral_harbor_trial", _fake_ephemeral
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )

    task_path = tmp_path / "task"
    task_path.mkdir()
    asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.GKE,
            harbor_config={
                "variant_id": "ephemeral",
                "source": "https://github.com/dot-agi/harbor",
                "resolved_sha": "a" * 40,
            },
        )
    )

    multiplier = captured["environment_build_timeout_multiplier"]
    assert multiplier is not None
    base = harbor_runner._ENV_BUILD_TIMEOUT_BASE_SEC
    assert (
        multiplier * base
        >= harbor_runner.settings.gke_pod_ready_timeout_sec
        + harbor_runner._GKE_ENV_BUILD_OVERHEAD_SEC
    )


def test_ephemeral_non_gke_trial_passes_caller_env_build_multiplier_through(
    tmp_path, monkeypatch
):
    # Off GKE the ephemeral path must not fabricate a multiplier: the caller's
    # value (None here) flows straight through, exactly as before the fix.
    from harbor.models.environment_type import EnvironmentType
    import oddish.workers.harbor.ephemeral as harbor_ephemeral

    captured: dict = {}

    async def _fake_ephemeral(**kwargs):
        captured.update(kwargs)
        return harbor_runner.HarborOutcome(
            reward=None,
            error=None,
            exit_code=0,
            duration_sec=0.0,
            job_result_path=None,
            job_dir=None,
        )

    monkeypatch.setattr(
        harbor_ephemeral, "run_ephemeral_harbor_trial", _fake_ephemeral
    )
    monkeypatch.setattr(
        harbor_runner, "validate_task_timeout_config", lambda path: None
    )

    task_path = tmp_path / "task"
    task_path.mkdir()
    asyncio.run(
        harbor_runner.run_harbor_trial_async(
            task_path=task_path,
            agent="nop",
            jobs_dir=tmp_path / "jobs",
            environment=EnvironmentType.DAYTONA,
            harbor_config={
                "variant_id": "ephemeral",
                "source": "https://github.com/dot-agi/harbor",
                "resolved_sha": "a" * 40,
            },
        )
    )

    assert captured["environment_build_timeout_multiplier"] is None
