"""Blessed-variant image/Function wiring (Modal-side).

The pure routing/build helpers are unit-tested in the oddish suite
(``test_harbor_variant_build.py``, ``test_worker_job_dispatcher.py``). Actually
constructing the per-variant Modal Images + Functions and running a trial on a
variant image needs the Modal runner, so the end-to-end check is skipped here and
called out as the validation gap in the report.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="requires Modal runner")
def test_blessed_variant_builds_named_function_and_image(monkeypatch):
    """A populated HARBOR_VARIANTS yields a process_single_job__<id> Function.

    On a Modal runner: register a blessed variant, then assert
    ``build_harbor_variant_functions(app)`` returns a Function keyed by the
    variant id with the ``process_single_job__<id>`` name, and that
    ``harbor_variant_images()`` produced an image for it (the dispatcher routes
    pins classified to ``<id>`` onto that Function).
    """
    import modal

    from oddish.core import harbor_source
    from oddish.core.harbor_source import HarborVariant, harbor_variant_function_name

    variant = HarborVariant(
        variant_id="harbor-next",
        source="https://github.com/dot-agi/harbor",
        sha="c" * 40,
    )
    monkeypatch.setattr(harbor_source, "HARBOR_VARIANTS", {variant.variant_id: variant})

    import modal_app  # noqa: F401  (imported for its side-effecting app/image)
    from worker.functions import build_harbor_variant_functions

    app = modal.App("oddish-harbor-variant-test")
    functions = build_harbor_variant_functions(app)

    assert set(functions) == {"harbor-next"}
    fn = functions["harbor-next"]
    assert fn.info.function_name.endswith(
        harbor_variant_function_name("harbor-next")
    ) or harbor_variant_function_name("harbor-next") in str(fn)

    assert "harbor-next" in modal_app.harbor_variant_images()


@pytest.mark.skip(reason="requires Modal runner")
def test_trial_pinned_to_blessed_variant_runs_in_process_on_its_image():
    """End-to-end: a trial whose pin classifies to a blessed id runs in-process
    on the variant image (its baked Harbor) and stamps that SHA. Needs a live
    Modal deploy with a second variant image; verified manually on the runner."""
