import ast
from pathlib import Path

import pytest

from oddish.db import WorkerJobKind
from oddish.workers.jobs import clear_handlers, ensure_builtin_handlers_registered
from oddish.workers.jobs.registry import get_handler

from worker.analyzer_sandbox import (
    SandboxAnalyzerJobHandler,
    install_sandbox_analyzer_handler,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_handlers()
    yield
    clear_handlers()


def test_install_overrides_the_core_analyzer_handler(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "analyzer_sandbox_enabled", True)
    ensure_builtin_handlers_registered()
    assert install_sandbox_analyzer_handler() is True
    assert isinstance(get_handler(WorkerJobKind.ANALYZER), SandboxAnalyzerJobHandler)


def test_kill_switch_leaves_the_core_handler_in_place(monkeypatch):
    from oddish.config import settings
    from oddish.workers.jobs.handlers import AnalyzerJobHandler

    monkeypatch.setattr(settings, "analyzer_sandbox_enabled", False)
    ensure_builtin_handlers_registered()
    assert install_sandbox_analyzer_handler() is False
    handler = get_handler(WorkerJobKind.ANALYZER)
    assert isinstance(handler, AnalyzerJobHandler)
    assert not isinstance(handler, SandboxAnalyzerJobHandler)


def test_later_ensure_builtin_call_cannot_clobber_the_override(monkeypatch):
    """worker_job_single_job also calls ensure_builtin defensively; it must
    early-return rather than reinstate the core handler."""
    from oddish.config import settings

    monkeypatch.setattr(settings, "analyzer_sandbox_enabled", True)
    ensure_builtin_handlers_registered()
    install_sandbox_analyzer_handler()
    ensure_builtin_handlers_registered()
    assert isinstance(get_handler(WorkerJobKind.ANALYZER), SandboxAnalyzerJobHandler)


def _module_level_call_lineno(tree: ast.Module, func_name: str) -> int | None:
    """Line number of a module-scope call to ``func_name``, whether it's a bare
    expression statement or the test of an ``if`` guard. Returns None if no such
    call exists at module scope (nested calls, e.g. inside a def, don't count)."""

    def call_target(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id
        return None

    for node in tree.body:
        if isinstance(node, ast.Expr) and call_target(node.value) == func_name:
            return node.lineno
        if isinstance(node, ast.If) and call_target(node.test) == func_name:
            return node.lineno
    return None


def test_functions_module_wires_sandbox_analyzer_after_builtin_handlers_registered():
    """The other tests in this file call install_sandbox_analyzer_handler() directly,
    so they pass regardless of whether worker/functions.py -- the module that actually
    runs at worker container load -- ever calls it. That wiring is what turns the
    sandbox-per-cohort analyzer on in production, and had zero coverage: deleting the
    `if install_sandbox_analyzer_handler():` block from functions.py left the other
    three tests green. This reads functions.py's source with ast instead of importing
    it, since the module wires up Modal/DB config as an import side effect.

    Ordering matters too: ensure_builtin_handlers_registered() must run first, or the
    defensive re-registration would clobber the sandbox override right back to the
    core handler.
    """
    source_path = Path(__file__).resolve().parent.parent / "worker" / "functions.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))

    ensure_builtin_line = _module_level_call_lineno(
        tree, "ensure_builtin_handlers_registered"
    )
    install_sandbox_line = _module_level_call_lineno(
        tree, "install_sandbox_analyzer_handler"
    )

    assert ensure_builtin_line is not None, (
        "functions.py must call ensure_builtin_handlers_registered() at module scope"
    )
    assert install_sandbox_line is not None, (
        "functions.py must call install_sandbox_analyzer_handler() at module scope "
        "to actually wire the sandbox-per-cohort analyzer into the running worker"
    )
    assert install_sandbox_line > ensure_builtin_line, (
        "install_sandbox_analyzer_handler() must run after "
        "ensure_builtin_handlers_registered(), or the defensive re-registration "
        "would clobber the sandbox override"
    )
