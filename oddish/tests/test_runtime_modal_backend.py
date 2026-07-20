from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.runtime.backends.modal import ModalBackend


def test_modal_backend_name_matches_environment_value() -> None:
    assert ModalBackend().name == "modal"


def test_modal_capabilities_advertise_gpu_and_private_registry() -> None:
    caps = ModalBackend().capabilities()
    assert caps.gpu is not None
    assert caps.gpu.max_count >= 1
    assert caps.private_registry_pull is True
    assert caps.cold_start == "minutes"
    assert caps.memory_snapshot_fork is True


def test_modal_harbor_env_kwargs_passes_caller_keys_through() -> None:
    base = {"region": "us-east", "keep": "value"}
    merged = ModalBackend().harbor_env_kwargs(dict(base))
    for key, value in base.items():
        assert merged[key] == value


def test_modal_env_kwargs_default_indicative_app_name(monkeypatch):
    # Sandboxes otherwise land in harbor's default app "__harbor__" — useless
    # in the Modal dashboard. Group them under <deployed-app>-trials instead.
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-611")
    merged = ModalBackend().harbor_env_kwargs({})
    assert merged["app_name"] == "oddish-pr-611-trials"


def test_modal_env_kwargs_app_name_fallback_without_env(monkeypatch):
    monkeypatch.delenv("MODAL_APP_NAME", raising=False)
    merged = ModalBackend().harbor_env_kwargs({})
    assert merged["app_name"] == "oddish-trials"


def test_modal_env_kwargs_caller_app_name_wins(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-prod")
    merged = ModalBackend().harbor_env_kwargs({"app_name": "custom"})
    assert merged["app_name"] == "custom"
