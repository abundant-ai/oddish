from __future__ import annotations

import asyncio
import os
from builtins import ExceptionGroup
from collections import namedtuple
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers import harbor_runner  # noqa: E402
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

    async def _fake_maybe_start_analysis_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_analysis_stage", _fake_maybe_start_analysis_stage
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

    async def _fake_maybe_start_analysis_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_analysis_stage", _fake_maybe_start_analysis_stage
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

    assert agent_config.model_name == "oddish-gpt"
    assert "AZURE_OPENAI_API_KEY" not in agent_config.env
    assert "OPENAI_API_KEY" not in agent_config.env


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

    async def _fake_maybe_start_analysis_stage(session, trial_id: str) -> bool:
        return False

    async def _fake_enqueue_analysis_worker_job(*args, **kwargs) -> None:
        return None

    import oddish.queue as queue_module

    monkeypatch.setattr(trial_handler, "_trial_session", _fake_trial_session)
    monkeypatch.setattr(
        queue_module, "maybe_start_analysis_stage", _fake_maybe_start_analysis_stage
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
