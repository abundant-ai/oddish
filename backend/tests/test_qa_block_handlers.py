import pytest

from oddish.db import WorkerJobKind
from oddish.workers.jobs import clear_handlers, ensure_builtin_handlers_registered
from oddish.workers.jobs.handlers import QaJobHandler
from oddish.workers.jobs.registry import get_handler

from worker.pre_trial_synth import pre_trial_block_synth
from worker.verdict_synth import verdict_block_synth
from worker.qa_block_handlers import BlockQaJobHandler, install_block_qa_handlers


@pytest.fixture(autouse=True)
def _clean():
    clear_handlers()
    yield
    clear_handlers()


def test_neither_flag_registers_nothing(monkeypatch):
    """Preserves today's default behavior exactly: with both flags off, the
    core QaJobHandler stays in place and install_block_qa_handlers is a
    no-op."""
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", False)
    monkeypatch.setattr(settings, "pre_trial_via_analyzer_block", False)
    ensure_builtin_handlers_registered()

    assert install_block_qa_handlers() is None
    handler = get_handler(WorkerJobKind.QA)
    assert isinstance(handler, QaJobHandler)
    assert not isinstance(handler, BlockQaJobHandler)


def test_verdict_only_resolves_verdict_block_synth_and_legacy_pre_trial(monkeypatch):
    from oddish.config import settings
    from oddish.workers.queue.qa_handler import default_pre_trial_synth

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", True)
    monkeypatch.setattr(settings, "pre_trial_via_analyzer_block", False)
    ensure_builtin_handlers_registered()

    handler = install_block_qa_handlers()
    assert handler is not None
    assert handler.verdict_synth_fn is verdict_block_synth
    assert handler.pre_trial_synth_fn is default_pre_trial_synth
    assert get_handler(WorkerJobKind.QA) is handler


def test_pre_trial_only_resolves_pre_trial_block_synth_and_legacy_verdict(monkeypatch):
    from oddish.config import settings
    from oddish.workers.queue.qa_handler import default_verdict_synth

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", False)
    monkeypatch.setattr(settings, "pre_trial_via_analyzer_block", True)
    ensure_builtin_handlers_registered()

    handler = install_block_qa_handlers()
    assert handler is not None
    assert handler.pre_trial_synth_fn is pre_trial_block_synth
    assert handler.verdict_synth_fn is default_verdict_synth
    assert get_handler(WorkerJobKind.QA) is handler


def test_both_flags_resolve_both_block_synths(monkeypatch):
    """The regression test: with BOTH flags enabled, the registered handler
    must carry BOTH block synths. Before the fix, registering
    VerdictBlockQaJobHandler then PreTrialBlockQaJobHandler (each
    override=True) had the second registration silently clobber the first
    -- verdict synthesis reverted to the legacy path with no error."""
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", True)
    monkeypatch.setattr(settings, "pre_trial_via_analyzer_block", True)
    ensure_builtin_handlers_registered()

    handler = install_block_qa_handlers()
    assert handler is not None
    assert handler.verdict_synth_fn is verdict_block_synth
    assert handler.pre_trial_synth_fn is pre_trial_block_synth
    assert get_handler(WorkerJobKind.QA) is handler


def test_synth_fns_are_not_bound_to_the_handler_instance(monkeypatch):
    """Guards the instance-attribute mechanism itself: verdict_synth_fn /
    pre_trial_synth_fn must stay plain functions (not bound methods), since
    run_task_qa_job calls them directly with no leading `self` argument."""
    from oddish.config import settings

    monkeypatch.setattr(settings, "verdict_via_analyzer_block", True)
    monkeypatch.setattr(settings, "pre_trial_via_analyzer_block", True)
    ensure_builtin_handlers_registered()

    handler = install_block_qa_handlers()
    assert handler is not None
    assert not hasattr(handler.verdict_synth_fn, "__self__")
    assert not hasattr(handler.pre_trial_synth_fn, "__self__")
