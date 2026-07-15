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
