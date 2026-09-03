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
    assert settings.thunder_capacity_fallback is False
    assert settings.thunder_fallback_provider == "modal"


def test_thunder_capacity_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDISH_THUNDER_ENABLED", "true")
    monkeypatch.setenv("ODDISH_THUNDER_MAX_CAPACITY", "7")
    monkeypatch.setenv("ODDISH_THUNDER_CAPACITY_FALLBACK", "T")
    monkeypatch.setenv("ODDISH_THUNDER_FALLBACK_PROVIDER", "modal")

    settings = Settings(_env_file=None)

    assert settings.thunder_enabled is True
    assert settings.thunder_max_capacity == 7
    assert settings.thunder_capacity_fallback is True
    assert settings.thunder_fallback_provider == "modal"


@pytest.mark.parametrize("capacity", [0, -1])
def test_thunder_capacity_must_be_positive(capacity: int) -> None:
    with pytest.raises(ValidationError, match="thunder_max_capacity"):
        Settings(_env_file=None, thunder_max_capacity=capacity)


def test_thunder_fallback_provider_is_normalized() -> None:
    settings = Settings(_env_file=None, thunder_fallback_provider=" Daytona ")

    assert settings.thunder_fallback_provider == "daytona"


@pytest.mark.parametrize("provider", ["", "  ", "thunder", "ec2", "not-a-provider"])
def test_thunder_fallback_provider_must_name_a_different_provider(
    provider: str,
) -> None:
    with pytest.raises(ValidationError, match="thunder_fallback_provider"):
        Settings(_env_file=None, thunder_fallback_provider=provider)
