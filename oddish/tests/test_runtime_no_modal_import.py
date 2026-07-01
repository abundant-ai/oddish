from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

# The import hook both helpers install: any module-load-time ``import modal``
# (top-level or submodule) raises. Kept as one source so the negative tests
# and the positive-control test below exercise the SAME guard mechanism.
_GUARD_HOOK = (
    "import builtins\n"
    "_real = builtins.__import__\n"
    "def _guard(name, *a, **k):\n"
    "    if name == 'modal' or name.startswith('modal.'):\n"
    "        raise AssertionError('modal imported at module load: ' + name)\n"
    "    return _real(name, *a, **k)\n"
    "builtins.__import__ = _guard\n"
)


def _import_without_modal(module: str) -> str:
    """Import ``module`` in a subprocess where importing ``modal`` would fail,
    returning the subprocess stdout ('ok' on success)."""
    code = (
        f"import sys; sys.path.insert(0, {SRC!r})\n"
        f"{_GUARD_HOOK}"
        f"import {module}\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_ports_import_without_modal() -> None:
    assert _import_without_modal("oddish.runtime.ports") == "ok"


def test_daytona_backend_imports_without_modal() -> None:
    assert _import_without_modal("oddish.runtime.backends.daytona") == "ok"


def test_registry_imports_without_modal() -> None:
    # The registry constructs ModalBackend(), but the Modal SDK import must stay
    # lazy (inside teardown/capture_diagnostics), so this must succeed.
    assert _import_without_modal("oddish.runtime.registry") == "ok"


def test_routing_imports_without_modal() -> None:
    assert _import_without_modal("oddish.runtime.routing") == "ok"


def test_guard_trips_on_eager_modal_import() -> None:
    """Positive control: an eager module-load ``import modal`` MUST trip the
    guard. Without this, a regression in the guard hook itself would let the
    four negative tests above pass vacuously."""
    code = f"{_GUARD_HOOK}import modal\nprint('ok')\n"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "modal imported at module load" in result.stderr
    assert result.stdout.strip() != "ok"
