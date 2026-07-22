import ast
from pathlib import Path

import pytest

from oddish.db import WorkerJobKind
from oddish.workers.jobs import clear_handlers, ensure_builtin_handlers_registered
from oddish.workers.jobs.handlers import QaJobHandler
from oddish.workers.jobs.registry import get_handler

from worker.verdict_synth import (
    VerdictBlockQaJobHandler,
    install_verdict_block_qa_handler,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_handlers()
    yield
    clear_handlers()


def test_flag_defaults_off():
    from oddish.config import settings

    assert settings.verdict_via_analyzer_block is False


def test_install_overrides_the_core_qa_handler(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", True)
    ensure_builtin_handlers_registered()
    assert install_verdict_block_qa_handler() is True
    assert isinstance(get_handler(WorkerJobKind.QA), VerdictBlockQaJobHandler)


def test_kill_switch_leaves_the_core_handler_in_place(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", False)
    ensure_builtin_handlers_registered()
    assert install_verdict_block_qa_handler() is False
    handler = get_handler(WorkerJobKind.QA)
    assert isinstance(handler, QaJobHandler)
    assert not isinstance(handler, VerdictBlockQaJobHandler)


def test_later_ensure_builtin_call_cannot_clobber_the_override(monkeypatch):
    """worker_job_single_job also calls ensure_builtin defensively; it must
    early-return rather than reinstate the core handler."""
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", True)
    ensure_builtin_handlers_registered()
    install_verdict_block_qa_handler()
    ensure_builtin_handlers_registered()
    assert isinstance(get_handler(WorkerJobKind.QA), VerdictBlockQaJobHandler)


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


def test_functions_module_wires_verdict_block_after_builtin_handlers_registered():
    """Mirrors test_analyzer_sandbox_registration.py's equivalent check: proves
    worker/functions.py -- the module that actually runs at worker container
    load -- calls install_block_qa_handlers() (the composed installer that
    wires in verdict_block_synth when settings.verdict_via_analyzer_block is
    on) at module scope, after ensure_builtin_handlers_registered() so the
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
        "actually wire the AnalyzerBlock-backed verdict synth into the running worker"
    )
    assert install_block_line > ensure_builtin_line, (
        "install_block_qa_handlers() must run after "
        "ensure_builtin_handlers_registered(), or the defensive re-registration "
        "would clobber the override"
    )
