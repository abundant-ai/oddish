from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.runtime.backends.modal import ModalBackend
from oddish.workers.harbor_modal_debug import _capture_modal_output


def _install_fake_modal(monkeypatch) -> dict:
    state: dict[str, object] = {"enabled": False}

    class _OutputManager:
        def __enter__(self):
            state["enabled"] = True
            return self

        def __exit__(self, *exc):
            return None

        def enable_image_logs(self):
            state["image_logs"] = True

        def set_timestamps(self, value):
            state["timestamps"] = value

    fake_modal = types.ModuleType("modal")
    fake_modal.enable_output = lambda: _OutputManager()
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    return state


def test_modal_capture_writes_log_and_enables_output(
    monkeypatch, tmp_path: Path
) -> None:
    state = _install_fake_modal(monkeypatch)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with ModalBackend().capture_diagnostics(job_dir) as log_path:
        assert log_path == job_dir / "modal-output.log"
        print("hello from trial")

    assert state["enabled"] is True
    contents = (job_dir / "modal-output.log").read_text(encoding="utf-8")
    assert "Capturing Modal SDK output" in contents
    assert "hello from trial" in contents


def test_capture_modal_output_noop_for_daytona(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with _capture_modal_output(job_dir, EnvironmentType.DAYTONA) as log_path:
        assert log_path is None
    assert not (job_dir / "modal-output.log").exists()


def test_capture_modal_output_delegates_to_modal_backend(
    monkeypatch, tmp_path: Path
) -> None:
    _install_fake_modal(monkeypatch)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with _capture_modal_output(job_dir, EnvironmentType.MODAL) as log_path:
        assert log_path == job_dir / "modal-output.log"
    assert (job_dir / "modal-output.log").exists()
