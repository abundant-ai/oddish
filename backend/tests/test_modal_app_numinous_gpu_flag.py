"""The Numinous GPU lane flag must be baked into the image env.

``ODDISH_NUMINOUS_GPU_ENABLED`` is read inside the worker by
``settings.numinous_gpu_enabled`` (oddish.runtime.backends.numinous) to decide
whether Numinous advertises GPU capacity. Like ``ODDISH_NUMINOUS_ENABLED`` and
the GKE coordinates, the worker re-reads settings from the image env, where
neither the deploy shell's env nor ``backend/.env`` exists. Without the bake, a
deploy that sets ``ODDISH_NUMINOUS_GPU_ENABLED=1`` never reaches API workers,
so capability negotiation keeps GPU trials on Modal (the bug Bugbot flagged on
PR #1407).
"""

from __future__ import annotations

import importlib

import modal_app


def test_gpu_flag_key_is_baked_into_env_vars():
    assert (
        modal_app._NUMINOUS_GPU_ENABLED_ENV
        == "ODDISH_NUMINOUS_GPU_ENABLED"
    )
    assert modal_app._NUMINOUS_GPU_ENABLED_ENV in modal_app.ENV_VARS


def test_gpu_flag_defaults_false_and_is_independent_of_enable(monkeypatch):
    # A CPU-only deploy (enable on, gpu off) must bake gpu=false so GPU
    # trials stay on Modal. The two flags are independent.
    monkeypatch.setenv("ODDISH_NUMINOUS_ENABLED", "1")
    monkeypatch.delenv("ODDISH_NUMINOUS_GPU_ENABLED", raising=False)
    importlib.reload(modal_app)
    try:
        assert modal_app.ENV_VARS[modal_app._NUMINOUS_GPU_ENABLED_ENV] == "false"
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)


def test_gpu_flag_bakes_true_when_deploy_sets_it(monkeypatch):
    monkeypatch.setenv("ODDISH_NUMINOUS_GPU_ENABLED", "1")
    importlib.reload(modal_app)
    try:
        assert modal_app.ENV_VARS[modal_app._NUMINOUS_GPU_ENABLED_ENV] == "true"
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)
