from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from pydantic import ValidationError

from oddish.config import Settings


def test_thunder_defaults_disabled_with_capacity_sixteen() -> None:
    settings = Settings(_env_file=None)

    assert settings.thunder_enabled is False
    assert settings.thunder_max_capacity == 16


def test_thunder_capacity_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDISH_THUNDER_ENABLED", "true")
    monkeypatch.setenv("ODDISH_THUNDER_MAX_CAPACITY", "7")

    settings = Settings(_env_file=None)

    assert settings.thunder_enabled is True
    assert settings.thunder_max_capacity == 7


@pytest.mark.parametrize("capacity", [0, -1])
def test_thunder_capacity_must_be_positive(capacity: int) -> None:
    with pytest.raises(ValidationError, match="thunder_max_capacity"):
        Settings(_env_file=None, thunder_max_capacity=capacity)
