"""Regression test for the GPU detection in `oddish run`.

`_task_config_requests_gpu` compared `environment.gpus > 0`. Tasks whose
task.toml omits `gpus` (e.g. schema_version 1.2) parse to `gpus=None`, which
made the comparison raise `TypeError: '>' not supported between 'NoneType'
and 'int'` and crash the whole submit. It must now treat an absent value as
0 GPUs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# oddish.cli re-exports a `run` *command* that shadows the submodule, so pull
# the actual module out of sys.modules via importlib.
run = importlib.import_module("oddish.cli.run")  # noqa: E402


def _patch_config(monkeypatch, gpus):
    monkeypatch.setattr(
        run,
        "HarborTaskConfig",
        SimpleNamespace(
            model_validate_toml=lambda _text: SimpleNamespace(
                environment=SimpleNamespace(gpus=gpus)
            )
        ),
    )


def test_none_gpus_does_not_crash(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("irrelevant; parser is patched")
    _patch_config(monkeypatch, None)
    # Previously raised TypeError; must now return False.
    assert run._task_config_requests_gpu(tmp_path) is False


def test_zero_gpus_is_false(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_config(monkeypatch, 0)
    assert run._task_config_requests_gpu(tmp_path) is False


def test_positive_gpus_is_true(tmp_path, monkeypatch):
    (tmp_path / "task.toml").write_text("x")
    _patch_config(monkeypatch, 2)
    assert run._task_config_requests_gpu(tmp_path) is True


def test_missing_task_toml_is_false(tmp_path):
    # No task.toml -> parse raises -> guarded to False (unchanged behavior).
    assert run._task_config_requests_gpu(tmp_path) is False
