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


def _wire_prompt_template(monkeypatch, tmp_path, content: str) -> None:
    """Point the synth's packaged-template path at a controlled file."""
    template = tmp_path / "pre_trial_qa.txt"
    template.write_text(content)
    monkeypatch.setattr(mod, "_PRE_TRIAL_PROMPT_PATH", template)


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


def test_packaged_pre_trial_prompt_exists():
    """The synth reads the packaged audit prompt; the file must ship with
    oddish and have real content."""
    content = mod._PRE_TRIAL_PROMPT_PATH.read_text()
    assert content.strip()


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
async def test_synth_substitutes_prompt_and_maps_action_items(monkeypatch, tmp_path):
    """Pure test of synthesize_task_pre_trial's load-bearing behaviors: it
    substitutes {task_id}/{trial_ids} into the packaged prompt template
    before handing it to AnalyzerBlock, maps `result.output["items"]` into a
    list of `ActionItem`, and carries the block's spend/id back out. The
    block/client/session are all faked -- no real sandbox, LLM, or DB."""

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    _wire_prompt_template(monkeypatch, tmp_path, "Audit {task_id}. Trials: {trial_ids}")
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
async def test_synth_maps_empty_items_to_empty_list(monkeypatch, tmp_path):
    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    class _EmptyAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            return _FakeAnalyzerResult({"items": []})

    _wire_prompt_template(monkeypatch, tmp_path, "Audit {task_id}. Trials: {trial_ids}")
    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)
    monkeypatch.setattr(mod, "AnalyzerBlock", _EmptyAnalyzerBlock)
    _wire_task_source(monkeypatch)

    result = await synthesize_task_pre_trial(
        "task_xyz", "task_xyz-v1", [], timeout=30.0
    )
    assert result.items == []


@pytest.mark.asyncio
async def test_synth_imposes_no_outer_deadline_around_the_block(monkeypatch, tmp_path):
    """The claude subprocess is bounded by CliConfig.timeout (which kills it
    cleanly on expiry). An outer asyncio.wait_for would race that inner timeout
    and, if it won, orphan the subprocess. Guard: a block.run() slower than the
    passed timeout still completes -- there is no outer cancellation."""

    async def fake_resolve_org_pre_trial(task_id):
        return "org_1", True

    class _SlowAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            # Longer than the tiny timeout passed below; a leftover outer
            # wait_for would cancel this and raise instead of returning.
            await asyncio.sleep(0.05)
            return _FakeAnalyzerResult({"items": []})

    _wire_prompt_template(monkeypatch, tmp_path, "Audit {task_id}.")
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
    async def fake_resolve_org_pre_trial(task_id):
        return None, True

    monkeypatch.setattr(mod, "_resolve_org_pre_trial", fake_resolve_org_pre_trial)

    with pytest.raises(RuntimeError, match="task_xyz"):
        await synthesize_task_pre_trial("task_xyz", "task_xyz-v1", [], timeout=30.0)
