import ast
import contextlib
from pathlib import Path

import pytest

from oddish.db import WorkerJobKind
from oddish.workers.jobs import clear_handlers, ensure_builtin_handlers_registered
from oddish.workers.jobs.handlers import QaJobHandler
from oddish.workers.jobs.registry import get_handler

import worker.pre_trial_synth as mod
from worker.pre_trial_synth import (
    PreTrialBlockQaJobHandler,
    install_pre_trial_block_qa_handler,
    pre_trial_block_synth,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_handlers()
    yield
    clear_handlers()


def test_flag_defaults_off():
    from oddish.config import settings

    assert settings.pre_trial_via_analyzer_block is False


def test_install_is_gated_off_by_default(monkeypatch):
    monkeypatch.setattr(mod.settings, "pre_trial_via_analyzer_block", False)
    assert mod.install_pre_trial_block_qa_handler() is False


def test_install_overrides_the_core_qa_handler(monkeypatch):
    monkeypatch.setattr(mod.settings, "pre_trial_via_analyzer_block", True)
    ensure_builtin_handlers_registered()
    assert install_pre_trial_block_qa_handler() is True
    assert isinstance(get_handler(WorkerJobKind.QA), PreTrialBlockQaJobHandler)


def test_kill_switch_leaves_the_core_handler_in_place(monkeypatch):
    monkeypatch.setattr(mod.settings, "pre_trial_via_analyzer_block", False)
    ensure_builtin_handlers_registered()
    assert install_pre_trial_block_qa_handler() is False
    handler = get_handler(WorkerJobKind.QA)
    assert isinstance(handler, QaJobHandler)
    assert not isinstance(handler, PreTrialBlockQaJobHandler)


def _module_level_call_lineno(tree: ast.Module, func_name: str) -> int | None:
    def call_target(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id
        return None

    for node in tree.body:
        if isinstance(node, ast.Expr) and call_target(node.value) == func_name:
            return node.lineno
        if isinstance(node, ast.If) and call_target(node.test) == func_name:
            return node.lineno
        if isinstance(node, ast.Assign) and call_target(node.value) == func_name:
            return node.lineno
    return None


def test_functions_module_wires_pre_trial_block_after_builtin_handlers_registered():
    """Mirrors test_verdict_synth_registration.py's equivalent check: proves
    worker/functions.py -- the module that actually runs at worker container
    load -- calls install_block_qa_handlers() (the composed installer that
    wires in pre_trial_block_synth when settings.pre_trial_via_analyzer_block
    is on) at module scope, after ensure_builtin_handlers_registered() so the
    defensive re-registration elsewhere can't clobber the override back to
    the core handler."""
    source_path = Path(__file__).resolve().parent.parent / "worker" / "functions.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    ensure_builtin_line = _module_level_call_lineno(
        tree, "ensure_builtin_handlers_registered"
    )
    install_block_line = _module_level_call_lineno(tree, "install_block_qa_handlers")

    assert ensure_builtin_line is not None, (
        "functions.py must call ensure_builtin_handlers_registered() at module scope"
    )
    assert install_block_line is not None, (
        "functions.py must call install_block_qa_handlers() at module scope to "
        "actually wire the AnalyzerBlock-backed pre-trial synth into the running worker"
    )
    assert install_block_line > ensure_builtin_line, (
        "install_block_qa_handlers() must run after "
        "ensure_builtin_handlers_registered(), or the defensive re-registration "
        "would clobber the override"
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
    canned result -- lets pre_trial_block_synth's prompt substitution and
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
async def test_pre_trial_block_synth_substitutes_prompt_and_maps_action_items(
    monkeypatch,
):
    """Pure test of pre_trial_block_synth's two load-bearing behaviors: it
    substitutes {task_id}/{trial_ids} into the DB-registry prompt template
    before handing it to AnalyzerBlock, and it maps `result.output["items"]`
    into a list of `ActionItem`. The block/client/session are all faked --
    no real sandbox, LLM, or DB."""

    async def fake_get_prompt_core(session, key):
        assert key == "pre_trial_qa"
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_id(task_id):
        return "org_1"

    fake_client = _FakeSandboxClient()

    async def fake_provision(**kwargs):
        assert kwargs["org_id"] == "org_1"
        return fake_client

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_id", fake_resolve_org_id)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", fake_provision)
    monkeypatch.setattr(mod, "AnalyzerBlock", _FakeAnalyzerBlock)

    items = await pre_trial_block_synth("task_xyz", ["t1", "t2"], timeout=30.0)

    prompt = _FakeAnalyzerBlock.last_kwargs["prompt"]
    assert prompt == "Audit task_xyz. Trials: t1, t2"
    assert _FakeAnalyzerBlock.last_kwargs["block_metadata"] == {
        "prompt_key": "pre_trial_qa",
        "prompt_version": 7,
    }

    assert len(items) == 1
    assert items[0].file == "verifier.py"
    assert items[0].line_start == 3
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_pre_trial_block_synth_maps_empty_items_to_empty_list(monkeypatch):
    async def fake_get_prompt_core(session, key):
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_id(task_id):
        return "org_1"

    async def fake_provision(**kwargs):
        return _FakeSandboxClient()

    class _EmptyAnalyzerBlock(_FakeAnalyzerBlock):
        async def run(self) -> _FakeAnalyzerResult:
            return _FakeAnalyzerResult({"items": []})

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_id", fake_resolve_org_id)
    monkeypatch.setattr(mod, "provision_oddish_sandbox_client", fake_provision)
    monkeypatch.setattr(mod, "AnalyzerBlock", _EmptyAnalyzerBlock)

    items = await pre_trial_block_synth("task_xyz", [], timeout=30.0)
    assert items == []


@pytest.mark.asyncio
async def test_pre_trial_block_synth_raises_when_org_id_unresolved(monkeypatch):
    async def fake_get_prompt_core(session, key):
        return None, _FakePromptVersion("Audit {task_id}. Trials: {trial_ids}")

    async def fake_resolve_org_id(task_id):
        return None

    monkeypatch.setattr(mod, "get_session", lambda: _fake_session_ctx())
    monkeypatch.setattr(mod, "get_prompt_core", fake_get_prompt_core)
    monkeypatch.setattr(mod, "_resolve_org_id", fake_resolve_org_id)

    with pytest.raises(RuntimeError, match="task_xyz"):
        await pre_trial_block_synth("task_xyz", [], timeout=30.0)
