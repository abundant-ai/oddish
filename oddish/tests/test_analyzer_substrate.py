"""Substrate selection: which execution backend an analyzer runs on.

The rule under test is that the substrate follows the analyzer's declared
capability needs, never whether the hosted sandbox module happens to be
importable in this process.
"""

import pytest

from oddish.blocks.analyzer.analyzer_block import AnalyzerType, resolve_substrate
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType


def test_post_trial_prefers_the_sandbox_when_one_is_available():
    assert (
        resolve_substrate(AnalyzerType.POST_TRIAL, sandbox_available=True)
        is LLMClientType.SANDBOX
    )


def test_post_trial_falls_back_to_worker_local_without_a_sandbox():
    # Self-host and local dev never register the hosted factory. Under the
    # default policy that must degrade to the worker-local subprocess rather
    # than failing classification outright.
    assert (
        resolve_substrate(AnalyzerType.POST_TRIAL, sandbox_available=False)
        is LLMClientType.CLAUDE_CLI
    )


def test_post_trial_never_policy_stays_worker_local_despite_availability():
    assert (
        resolve_substrate(
            AnalyzerType.POST_TRIAL, sandbox_available=True, sandbox_policy="never"
        )
        is LLMClientType.CLAUDE_CLI
    )


def test_post_trial_always_policy_without_a_backend_raises():
    # An operator who demanded isolation must not silently get a different
    # backend whose stream a different output transform parses.
    with pytest.raises(RuntimeError, match="sandbox"):
        resolve_substrate(
            AnalyzerType.POST_TRIAL, sandbox_available=False, sandbox_policy="always"
        )


def test_unknown_sandbox_policy_is_rejected():
    with pytest.raises(ValueError, match="sandbox_policy"):
        resolve_substrate(
            AnalyzerType.POST_TRIAL, sandbox_available=True, sandbox_policy="sometimes"
        )


def test_pre_trial_always_requires_the_sandbox():
    # Pre-trial runs `oddish pull` and the verifier, so isolation is a hard
    # requirement, not a preference.
    assert (
        resolve_substrate(AnalyzerType.PRE_TRIAL, sandbox_available=True)
        is LLMClientType.SANDBOX
    )


def test_pre_trial_without_a_sandbox_backend_raises():
    with pytest.raises(RuntimeError, match="sandbox"):
        resolve_substrate(AnalyzerType.PRE_TRIAL, sandbox_available=False)


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
async def test_classifier_uses_the_sandbox_by_default(monkeypatch, tmp_path):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, True)
    monkeypatch.setattr(settings, "post_trial_sandbox", "auto")

    block = await _run_post_trial_block(monkeypatch, tmp_path)

    assert block.llm_client_type is LLMClientType.SANDBOX
    assert block._sandbox_config.files_to_upload
    assert block._client_factory is None


@pytest.mark.asyncio
async def test_classifier_degrades_to_worker_local_without_a_sandbox(
    monkeypatch, tmp_path
):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, False)
    monkeypatch.setattr(settings, "post_trial_sandbox", "auto")

    block = await _run_post_trial_block(monkeypatch, tmp_path)

    assert block.llm_client_type is LLMClientType.CLAUDE_CLI
    assert block._sandbox_config is None
    assert block._client_factory is not None


@pytest.mark.asyncio
async def test_classifier_honours_the_never_policy(monkeypatch, tmp_path):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, True)
    monkeypatch.setattr(settings, "post_trial_sandbox", "never")

    block = await _run_post_trial_block(monkeypatch, tmp_path)

    assert block.llm_client_type is LLMClientType.CLAUDE_CLI


@pytest.mark.asyncio
async def test_classifier_raises_when_always_but_unavailable(monkeypatch, tmp_path):
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, False)
    monkeypatch.setattr(settings, "post_trial_sandbox", "always")

    with pytest.raises(RuntimeError, match="sandbox"):
        await _run_post_trial_block(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_sandbox_run_is_bounded_by_a_timeout(monkeypatch, tmp_path):
    """A hung sandbox must not stall the QA job until the worker lease expires.

    Every other block caller wraps ``block.run()`` in a timeout; post-trial is
    the one that did not.
    """
    import asyncio

    from oddish.analyze.classifier import TrialClassifier
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
    from oddish.config import settings

    _patch_sandbox_available(monkeypatch, True)
    monkeypatch.setattr(settings, "post_trial_sandbox", "auto")

    task_dir = tmp_path / "task"
    trial_dir = tmp_path / "trial"
    task_dir.mkdir()
    trial_dir.mkdir()
    (trial_dir / "result.json").write_text("{}")

    async def slower_than_the_timeout(self):
        # Deliberately finite: if the timeout regresses away, this returns None
        # and the caller fails fast on the missing .output rather than hanging
        # CI for an hour.
        await asyncio.sleep(2)

    monkeypatch.setattr(AnalyzerBlock, "run", slower_than_the_timeout)
    classifier = TrialClassifier(model="anthropic/test", timeout=0.2)

    with pytest.raises(TimeoutError):
        await classifier._run_in_analyzer_block(
            prompt="classify",
            trial_dir=trial_dir,
            task_dir=task_dir,
            extra_dirs=None,
            context={"trial_id": "trial-1", "task_id": "task-1"},
        )
