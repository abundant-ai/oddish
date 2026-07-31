"""Substrate selection: which execution backend an analyzer runs on.

The rule under test is that the substrate follows the analyzer's declared
capability needs, never whether the hosted sandbox module happens to be
importable in this process.
"""

import pytest

from oddish.blocks.analyzer.analyzer_block import AnalyzerType, resolve_substrate
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType


def test_post_trial_stays_worker_local_even_when_sandbox_is_available():
    # The regression this guards: keying the substrate off
    # sandbox_client_factory_registered() silently moved post-trial QA onto a
    # per-trial Daytona sandbox in the hosted worker, purely because
    # backend/worker/functions.py imports the sandbox client.
    assert (
        resolve_substrate(AnalyzerType.POST_TRIAL, sandbox_available=True)
        is LLMClientType.CLAUDE_CLI
    )


def test_post_trial_runs_in_sandbox_only_on_explicit_opt_in():
    assert (
        resolve_substrate(
            AnalyzerType.POST_TRIAL, sandbox_available=True, force_sandbox=True
        )
        is LLMClientType.SANDBOX
    )


def test_sandbox_opt_in_without_a_registered_backend_raises():
    # Fail loudly rather than degrading to a different backend with a different
    # output parser.
    with pytest.raises(RuntimeError, match="sandbox"):
        resolve_substrate(
            AnalyzerType.POST_TRIAL, sandbox_available=False, force_sandbox=True
        )


def test_pre_trial_runs_worker_local_like_post_trial():
    # Pre-trial now reads the task source the worker already downloaded
    # (Read/Glob over a local dir), the same as post-trial -- no sandbox and no
    # `oddish pull`, so nothing to install and no version pin to publish.
    assert (
        resolve_substrate(AnalyzerType.PRE_TRIAL, sandbox_available=True)
        is LLMClientType.CLAUDE_CLI
    )


def test_pre_trial_does_not_require_a_sandbox_backend():
    # It never needs a sandbox, so it must resolve even when none is registered.
    assert (
        resolve_substrate(AnalyzerType.PRE_TRIAL, sandbox_available=False)
        is LLMClientType.CLAUDE_CLI
    )


# ---- classifier wiring ----


def _patch_sandbox_available(monkeypatch, available: bool):
    from oddish.blocks.analyzer import analyzer_llm_client

    monkeypatch.setattr(
        analyzer_llm_client, "sandbox_client_factory_registered", lambda: available
    )


async def _run_post_trial_block(monkeypatch, tmp_path):
    """Drive the classifier's block construction, capturing the built block."""
    from types import SimpleNamespace

    from oddish.analyze.classifier import TrialClassifier
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock

    task_dir = tmp_path / "task"
    trial_dir = tmp_path / "trial"
    task_dir.mkdir()
    trial_dir.mkdir()
    (task_dir / "README.md").write_text("task")
    (trial_dir / "result.json").write_text("{}")

    captured = {}

    async def fake_run(self):
        captured["block"] = self
        return SimpleNamespace(output={"classification": "GOOD_SUCCESS"})

    monkeypatch.setattr(AnalyzerBlock, "run", fake_run)
    classifier = TrialClassifier(model="anthropic/test")
    await classifier._run_in_analyzer_block(
        prompt=f"read {task_dir} and {trial_dir}",
        trial_dir=trial_dir,
        task_dir=task_dir,
        extra_dirs=None,
        context={"trial_id": "trial-1", "task_id": "task-1"},
    )
    return captured["block"]


@pytest.mark.asyncio
async def test_classifier_stays_local_when_sandbox_is_available_but_not_opted_in(
    monkeypatch, tmp_path
):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, True)
    monkeypatch.setattr(settings, "post_trial_sandbox_enabled", False)

    block = await _run_post_trial_block(monkeypatch, tmp_path)

    assert block.llm_client_type is LLMClientType.CLAUDE_CLI
    assert block._sandbox_config is None
    # No tar upload, and the prompt keeps the worker-local paths it was built with.
    assert block._cli_config is not None


@pytest.mark.asyncio
async def test_classifier_uses_sandbox_when_explicitly_enabled(monkeypatch, tmp_path):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, True)
    monkeypatch.setattr(settings, "post_trial_sandbox_enabled", True)

    block = await _run_post_trial_block(monkeypatch, tmp_path)

    assert block.llm_client_type is LLMClientType.SANDBOX
    assert block._sandbox_config.files_to_upload
    assert block._cli_config is None


@pytest.mark.asyncio
async def test_classifier_raises_when_sandbox_enabled_but_unavailable(
    monkeypatch, tmp_path
):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, False)
    monkeypatch.setattr(settings, "post_trial_sandbox_enabled", True)

    with pytest.raises(RuntimeError, match="sandbox"):
        await _run_post_trial_block(monkeypatch, tmp_path)
