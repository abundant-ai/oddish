from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _import_without_modal(module: str) -> str:
    """Import ``module`` in a subprocess where importing ``modal`` would fail,
    returning the subprocess stdout ('ok' on success)."""
    code = (
        "import sys; sys.path.insert(0, %r);\n"
        "import builtins;\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name == 'modal' or name.startswith('modal.'):\n"
        "        raise AssertionError('modal imported at module load: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import %s\n"
        "print('ok')\n"
    ) % (SRC, module)
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
