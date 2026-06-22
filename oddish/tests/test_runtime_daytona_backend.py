from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings
from oddish.runtime.backends.daytona import DaytonaBackend


def test_daytona_backend_name_matches_environment_value() -> None:
    assert DaytonaBackend().name == "daytona"


def test_daytona_capabilities_are_cpu_only_no_private_registry() -> None:
    caps = DaytonaBackend().capabilities()
    assert caps.gpu is None
    assert caps.private_registry_pull is False
    assert caps.cold_start == "seconds"


def test_daytona_env_kwargs_inject_autostop_autodelete_ephemeral() -> None:
    merged = DaytonaBackend().harbor_env_kwargs({})
    assert merged["auto_stop_interval_mins"] == settings.daytona_auto_stop_interval_mins
    assert (
        merged["auto_delete_interval_mins"]
        == settings.daytona_auto_delete_interval_mins
    )
    assert merged["ephemeral"] == settings.daytona_ephemeral


def test_daytona_env_kwargs_caller_overrides_win() -> None:
    # Caller-supplied kwargs must override the Daytona defaults (the original
    # ``{defaults, **caller}`` spread put the caller last).
    merged = DaytonaBackend().harbor_env_kwargs({"ephemeral": False, "extra": "x"})
    assert merged["ephemeral"] is False
    assert merged["extra"] == "x"
