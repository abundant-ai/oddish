"""Regression tests for the Modal runtime-secret list.

Deploying ``oddish`` from a working tree that had ``backend/.env`` present used
to append an ephemeral ``Secret.from_dict`` to ``runtime_secrets``. The deployed
function then recorded one more dependency object than each container computed at
boot (the image never ships ``.env``), so every container died at startup with
``Function has N dependencies but container got N+1 object ids`` and crash-looped.

The invariant that must hold: the number of Modal dependency objects in
``runtime_secrets`` must NOT depend on whether ``backend/.env`` exists, because
that is precisely what differs between the deploy host and the container.
"""

from __future__ import annotations

import importlib

import modal_app


def _reload_with_dotenv(monkeypatch, values: dict[str, str]):
    """Reload ``modal_app`` as if ``backend/.env`` contained ``values``.

    ``modal_app`` does ``from dotenv import dotenv_values``, so reload re-binds
    the name from ``dotenv``; patching it there makes the reloaded module read
    our fake ``.env`` contents.
    """
    monkeypatch.setattr("dotenv.dotenv_values", lambda *a, **k: values)
    return importlib.reload(modal_app)


def test_runtime_secret_count_independent_of_local_dotenv(monkeypatch):
    try:
        without_dotenv = len(_reload_with_dotenv(monkeypatch, {}).runtime_secrets)
        with_dotenv = len(
            _reload_with_dotenv(monkeypatch, {"FOO": "bar"}).runtime_secrets
        )
        assert with_dotenv == without_dotenv, (
            "runtime_secrets count changed when .env is present "
            f"({without_dotenv} -> {with_dotenv}); this crash-loops deployed "
            "containers, which never see backend/.env"
        )
    finally:
        monkeypatch.undo()
        importlib.reload(modal_app)
