import contextlib
from pathlib import Path

import pytest

import oddish.workers.queue.qa_handler as qa_handler
import worker.pre_trial_synth as mod
from worker.pre_trial_synth import synthesize_task_pre_trial


def test_flag_defaults_off():
    from oddish.config import settings

    assert settings.pre_trial_enabled is False


def test_importing_module_registers_the_pre_trial_hook():
    # Importing worker.pre_trial_synth runs register_pre_trial_synth(...) at
    # module scope, so core's qa_handler hook points at our synth. This is the
    # seam run_task_qa_job calls when settings.pre_trial_enabled.
    assert qa_handler._pre_trial_synth_fn is synthesize_task_pre_trial


def test_functions_module_imports_pre_trial_synth_for_its_side_effect():
    """worker/functions.py -- the module that runs at worker container load --
    must import worker.pre_trial_synth so the hook is registered in the running
    worker (import side effect)."""
    source = (
        Path(__file__).resolve().parent.parent / "worker" / "functions.py"
    ).read_text()
    assert "pre_trial_synth" in source, (
        "functions.py must import pre_trial_synth to register the pre-trial hook "
        "into the running worker"
    )


class _FakePromptVersion:
    def __init__(self, content: str, version: int = 7) -> None:
        self.content = content
        self.version = version


class _FakeSandboxClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeAnalyzerResult:
    def __init__(self, output: dict) -> None:
        self.output = output


class _FakeAnalyzerBlock:
    """Captures the kwargs AnalyzerBlock was constructed with and returns a
    canned result -- lets synthesize_task_pre_trial's prompt substitution and
    output mapping be tested with no real LLM/sandbox/DB."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = kwargs

    async def run(self) -> _FakeAnalyzerResult:
        return _FakeAnalyzerResult(
            {
                "items": [
                    {
                        "source": "pre_trial",
                        "problem_type": "incompleteness",
                        "dimension": "verifier",
                        "file": "verifier.py",
                        "line_start": 3,
                        "line_end": 5,
                        "title": "t",
                        "detail": "d",
                        "recommendation": "r",
                        "tier": "must_fix",
                    }
                ]
            }
        )


@contextlib.asynccontextmanager
async def _fake_session_ctx():
    yield None


@pytest.mark.asyncio
async def test_synth_substitutes_prompt_and_maps_action_items(monkeypatch):
    """Pure test of synthesize_task_pre_trial's two load-bearing behaviors: it
    substitutes {task_id}/{trial_ids} into the DB-registry prompt template
    before handing it to AnalyzerBlock, and it maps `result.output["items"]`
    into a list of `ActionItem`. The block/client/session are all faked --
    no real sandbox, LLM, or DB."""

    async def fake_get_prompt_core(session, kind):
        assert kind == "QA_PRE_TRIAL"
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    fake_client = _FakeSandboxClient()

    async def fake_provision(**kwargs):
        assert kwargs["org_id"] == "org_1"
        return fake_client

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", fake_provision)
    monkeypatch.setattr(mod, "AnalyzerBlock", _FakeAnalyzerBlock)

    items = await synthesize_task_pre_trial(
        "task_xyz", "task_xyz-v1", ["t1", "t2"], timeout=30.0
    )

    prompt = _FakeAnalyzerBlock.last_kwargs["prompt"]
    assert prompt == "Audit task_xyz. Trials: t1, t2"
    # The audited version is recorded on the block input for attribution.
    assert _FakeAnalyzerBlock.last_kwargs["input"].input["task_version_id"] == (
        "task_xyz-v1"
    )
    assert _FakeAnalyzerBlock.last_kwargs["block_metadata"] == {
        "prompt_key": "QA_PRE_TRIAL",
        "prompt_version": 7,
    }

    assert len(items) == 1
    assert items[0].file == "verifier.py"
    assert items[0].line_start == 3
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_synth_maps_empty_items_to_empty_list(monkeypatch):
    async def fake_get_prompt_core(session, key):
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    async def fake_provision(**kwargs):
        return _FakeSandboxClient()

    class _EmptyAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            return _FakeAnalyzerResult({"items": []})

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", fake_provision)
    monkeypatch.setattr(mod, "AnalyzerBlock", _EmptyAnalyzerBlock)

    items = await synthesize_task_pre_trial("task_xyz", "task_xyz-v1", [], timeout=30.0)
    assert items == []


@pytest.mark.asyncio
async def test_synth_returns_before_provisioning_when_org_disabled(monkeypatch):
    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", False

    async def fail_provision(**kwargs):
        raise AssertionError("disabled org must not provision a sandbox")

    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", fail_provision)

    assert await synthesize_task_pre_trial("task_1", "task_1-v1", [], 10) is None


@pytest.mark.asyncio
async def test_synth_raises_when_org_id_unresolved(monkeypatch):
    async def fake_get_prompt_core(session, key):
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return None, True

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)

    with pytest.raises(RuntimeError, match="task_xyz"):
        await synthesize_task_pre_trial("task_xyz", "task_xyz-v1", [], timeout=30.0)


@pytest.mark.asyncio
async def test_provisioning_is_time_bounded(monkeypatch):
    """Sandbox provisioning runs before the block-run wait_for, but it must
    not be unbounded: the claim lease is sized as pre_trial_timeout +
    PRE_TRIAL_LEASE_MARGIN_SECONDS, so a provisioning hang longer than the
    margin would let the wall clock outrun the lease. A hang must surface as
    TimeoutError (-> recorded as pre-trial failure), not run forever."""
    import asyncio

    async def fake_get_prompt_core(session, kind):
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    async def hanging_provision(**kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", hanging_provision)
    monkeypatch.setattr(mod, "_PROVISION_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        await synthesize_task_pre_trial("task_xyz", "task_xyz-v1", ["t1"], timeout=30.0)
