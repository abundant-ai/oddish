import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import oddish.workers.queue.qa_handler as qa_handler
import worker.pre_trial_synth as mod
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from worker.pre_trial_synth import synthesize_task_pre_trial


async def _fake_resolve_task_source_location(task_id, task_version_id=None):
    return None, f"/fake/{task_id}/path"


async def _fake_resolve_task_directory(task_id, *, task_s3_key, task_path):
    return Path(task_path or f"/fake/{task_id}/dir"), None, None


def _wire_task_source(monkeypatch):
    """Fake the source lookup + task download so the synth builds its CLAUDE_CLI
    block without a real task-version row or S3 object."""
    monkeypatch.setattr(
        mod, "resolve_task_source_location", _fake_resolve_task_source_location
    )
    monkeypatch.setattr(mod, "resolve_task_directory", _fake_resolve_task_directory)


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


class _FakePrompt:
    def __init__(self, id: str = "prompt_fake_id") -> None:
        self.id = id


class _FakePromptVersion:
    def __init__(self, content: str, version: int = 7) -> None:
        self.content = content
        self.version = version


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
        # Real AnalyzerBlock sets both in __init__; the synth reads them to
        # attach the audit's spend to the version it audited.
        self.id = "block_pre_synth"
        self.usage = SimpleNamespace(cost_usd=0.146)

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
    """Pure test of synthesize_task_pre_trial's load-bearing behaviors: it
    substitutes {task_id}/{trial_ids} into the DB-registry prompt template
    before handing it to AnalyzerBlock, maps `result.output["items"]` into a
    list of `ActionItem`, and carries the block's spend/id back out. The
    block/client/session are all faked -- no real sandbox, LLM, or DB."""

    async def fake_resolve_prompt_core(session, kind, **scope):
        assert kind == "QA_PRE_TRIAL"
        assert scope == {
            "org_id": "org_1",
            "user_id": None,
            "experiment_id": None,
            "task_id": "task_xyz",
            "trial_id": None,
        }
        return _FakePrompt(), _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "resolve_prompt_core", fake_resolve_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "AnalyzerBlock", _FakeAnalyzerBlock)
    _wire_task_source(monkeypatch)

    result = await synthesize_task_pre_trial(
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
        "prompt_id": "prompt_fake_id",
    }
    # Runs worker-local (CLAUDE_CLI) over the downloaded task dir -- no sandbox,
    # no oddish-CLI install.
    assert _FakeAnalyzerBlock.last_kwargs["llm_client_type"] is LLMClientType.CLAUDE_CLI
    assert "sandbox_config" not in _FakeAnalyzerBlock.last_kwargs
    assert (
        str(_FakeAnalyzerBlock.last_kwargs["cli_config"].cwd) == "/fake/task_xyz/path"
    )
    output_schema = json.loads(
        _FakeAnalyzerBlock.last_kwargs["cli_config"].json_schema
    )
    assert output_schema["type"] == "object"
    assert "items" in output_schema["properties"]

    assert len(result.items) == 1
    assert result.items[0].file == "verifier.py"
    assert result.items[0].line_start == 3
    # The audit's spend and the handle for its raw S3 output ride back with the
    # findings -- analysis_costs rows carry no version reference, so this is the
    # only place they can be tied to the version being audited.
    assert result.cost_usd == 0.146
    assert result.block_id == "block_pre_synth"


@pytest.mark.asyncio
async def test_synth_maps_empty_items_to_empty_list(monkeypatch):
    async def fake_resolve_prompt_core(session, key, **scope):
        return _FakePrompt(), _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    class _EmptyAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            return _FakeAnalyzerResult({"items": []})

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "resolve_prompt_core", fake_resolve_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "AnalyzerBlock", _EmptyAnalyzerBlock)
    _wire_task_source(monkeypatch)

    result = await synthesize_task_pre_trial(
        "task_xyz", "task_xyz-v1", [], timeout=30.0
    )
    assert result.items == []


@pytest.mark.asyncio
async def test_synth_imposes_no_outer_deadline_around_the_block(monkeypatch):
    """The claude subprocess is bounded by CliConfig.timeout (which kills it
    cleanly on expiry). An outer asyncio.wait_for would race that inner timeout
    and, if it won, orphan the subprocess. Guard: a block.run() slower than the
    passed timeout still completes -- there is no outer cancellation."""

    async def fake_resolve_prompt_core(session, key, **scope):
        return _FakePrompt(), _FakePromptVersion("Audit {task_id}.")

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    class _SlowAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            # Longer than the tiny timeout passed below; a leftover outer
            # wait_for would cancel this and raise instead of returning.
            await asyncio.sleep(0.05)
            return _FakeAnalyzerResult({"items": []})

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "resolve_prompt_core", fake_resolve_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "AnalyzerBlock", _SlowAnalyzerBlock)
    _wire_task_source(monkeypatch)

    result = await synthesize_task_pre_trial(
        "task_xyz", "task_xyz-v1", [], timeout=0.01
    )
    assert result.items == []


@pytest.mark.asyncio
async def test_synth_returns_before_provisioning_when_org_disabled(monkeypatch):
    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", False

    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(
        mod,
        "AnalyzerBlock",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled org must not construct a block")
        ),
    )

    assert await synthesize_task_pre_trial("task_1", "task_1-v1", [], 10) is None


@pytest.mark.asyncio
async def test_synth_raises_when_org_id_unresolved(monkeypatch):
    async def fake_resolve_prompt_core(session, key, **scope):
        return _FakePrompt(), _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_pre_trial(task_id):
        return None, True

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "resolve_prompt_core", fake_resolve_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)

    with pytest.raises(RuntimeError, match="task_xyz"):
        await synthesize_task_pre_trial("task_xyz", "task_xyz-v1", [], timeout=30.0)


# ---------------------------------------------------------------------------
# resolve_prompt_core scope resolution (real DB, only AnalyzerBlock/org faked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synth_prefers_task_scoped_prompt_over_global(monkeypatch):
    """Guard: a task-scoped QA_PRE_TRIAL override must be used instead of the
    global prompt. Exercises resolve_prompt_core for real against the DB."""
    import uuid

    from oddish.core.prompts import set_prompt_core
    from oddish.db import PromptKind, PromptModel, get_session

    task_id = f"test_pre_trial_scope_{uuid.uuid4().hex[:8]}"
    org_id = "org_pre_trial_scope_test"

    async with get_session() as session:
        await set_prompt_core(
            session,
            kind=PromptKind.QA_PRE_TRIAL.value,
            content="SCOPED Audit {task_id}. Trials: {trial_ids}",
            scope_type="task",
            scope_id=task_id,
            org_id=org_id,
        )
        await session.commit()

    async def fake_resolve_org_pre_trial(tid):
        return org_id, True

    try:
        monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
        monkeypatch.setattr(mod, "AnalyzerBlock", _FakeAnalyzerBlock)
        _wire_task_source(monkeypatch)

        await synthesize_task_pre_trial(task_id, f"{task_id}-v1", ["t1"], timeout=30.0)

        prompt = _FakeAnalyzerBlock.last_kwargs["prompt"]
        assert prompt == f"SCOPED Audit {task_id}. Trials: t1"
    finally:
        async with get_session() as session:
            await session.execute(
                PromptModel.__table__.delete().where(
                    PromptModel.kind == PromptKind.QA_PRE_TRIAL.value,
                    PromptModel.scope_type == "task",
                    PromptModel.scope_id == task_id,
                )
            )
            await session.commit()


@pytest.mark.asyncio
async def test_synth_falls_back_to_global_with_no_scoped_rows(monkeypatch):
    """Behavior preservation: an org/task with no scoped QA_PRE_TRIAL rows
    must still resolve the global prompt -- the property that must not
    regress when the lookup becomes scope-aware."""
    from oddish.core.prompt_seeds import seed_prompts
    from oddish.core.prompts import get_prompt_core
    from oddish.db import PromptKind, get_session

    # Guarantee the global row exists regardless of what other tests in the
    # suite have deleted or re-seeded before this one runs.
    async with get_session() as session:
        await seed_prompts(session)
        await session.commit()

    async with get_session() as session:
        global_prompt, global_version = await get_prompt_core(
            session, PromptKind.QA_PRE_TRIAL.value
        )

    async def fake_resolve_org_pre_trial(tid):
        return "org_with_no_overrides", True

    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "AnalyzerBlock", _FakeAnalyzerBlock)
    _wire_task_source(monkeypatch)

    await synthesize_task_pre_trial(
        "task_with_no_overrides", "task_with_no_overrides-v1", ["t1"], timeout=30.0
    )

    assert _FakeAnalyzerBlock.last_kwargs["block_metadata"] == {
        "prompt_key": "QA_PRE_TRIAL",
        "prompt_version": global_version.version,
        "prompt_id": global_prompt.id,
    }
    expected_prompt = global_version.content.replace(
        "{task_id}", "task_with_no_overrides"
    ).replace("{trial_ids}", "t1")
    assert _FakeAnalyzerBlock.last_kwargs["prompt"] == expected_prompt
