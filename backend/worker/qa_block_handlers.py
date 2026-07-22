"""Composes the AnalyzerBlock-backed verdict and pre-trial QA synth
overrides onto a SINGLE ``QaJobHandler`` instance, keyed independently off
``settings.verdict_via_analyzer_block`` / ``settings.pre_trial_via_analyzer_block``.

``verdict_synth.py`` and ``pre_trial_synth.py`` each register their own
subclass with ``override=True`` -- fine individually, but the registry
(``oddish.workers.jobs.registry.HANDLERS``) holds at most one handler per
kind, so registering both back to back has the second call replace the
first handler wholesale. Since each subclass only overrides its own synth
attr and inherits the legacy default for the other, "both flags on" ends
up silently reverting whichever synth was overridden first. Building one
handler here and setting only the flag-enabled attrs avoids that clobber.
"""

from __future__ import annotations

from oddish.config import settings
from oddish.workers.jobs.handlers import QaJobHandler

from .pre_trial_synth import pre_trial_block_synth
from .verdict_synth import verdict_block_synth


class BlockQaJobHandler(QaJobHandler):
    """Same QA orchestration as QaJobHandler -- ``install_block_qa_handlers``
    sets whichever of ``verdict_synth_fn`` / ``pre_trial_synth_fn`` its flag
    enables as an instance attribute, leaving the other at its inherited
    legacy default."""


def install_block_qa_handlers() -> QaJobHandler | None:
    """Register ONE QA handler carrying every flag-enabled block synth.

    Returns the registered handler, or None (registering nothing, so the
    core ``QaJobHandler`` stays in place) when neither flag is on --
    matching today's default behavior exactly.
    """
    from oddish.workers.jobs import register

    verdict_on = settings.verdict_via_analyzer_block
    pre_trial_on = settings.pre_trial_via_analyzer_block
    if not verdict_on and not pre_trial_on:
        return None

    handler = BlockQaJobHandler()
    if verdict_on:
        # Instance attribute, NOT staticmethod: unlike a class attribute, a
        # plain function stashed in instance.__dict__ is returned as-is on
        # `self.verdict_synth_fn` lookup -- the descriptor protocol that
        # would bind it to `self` only fires when the attribute is resolved
        # off the class. Confirmed against QaJobHandler.run's
        # `self.verdict_synth_fn` usage.
        handler.verdict_synth_fn = verdict_block_synth
    if pre_trial_on:
        handler.pre_trial_synth_fn = pre_trial_block_synth

    register(handler, override=True)
    return handler
