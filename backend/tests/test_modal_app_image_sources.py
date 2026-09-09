"""Startup dependencies must be copied into every deployed backend image."""

import pytest

from oddish.core.harbor_source import HARBOR_VARIANTS


@pytest.mark.parametrize("variant", [None, *HARBOR_VARIANTS.values()])
def test_image_contains_org_approval_module(monkeypatch, variant):
    import modal_app

    copied_modules = set()
    original = modal_app.modal.Image.add_local_python_source

    def capture_sources(image, *modules, **kwargs):
        if kwargs.get("copy"):
            copied_modules.update(modules)
        return original(image, *modules, **kwargs)

    monkeypatch.setattr(
        modal_app.modal.Image, "add_local_python_source", capture_sources
    )
    modal_app._build_worker_image(variant)

    # Both API auth and worker startup import this top-level module. uv_sync
    # installs dependencies only, so setuptools py-modules cannot supply it.
    assert "org_access" in copied_modules
