from decimal import Decimal

import pytest
from pydantic import ValidationError

from oddish.config import QuotaMode, Settings


def test_quota_defaults_are_enforced_at_two_hundred_usd(monkeypatch):
    monkeypatch.delenv("ODDISH_DEFAULT_DAILY_QUOTA_USD", raising=False)
    monkeypatch.delenv("ODDISH_QUOTA_MODE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.default_daily_quota_usd == Decimal("200.00")
    assert settings.quota_pause_remaining_percent == Decimal("80")
    assert settings.quota_pause_remaining_usd is None
    assert settings.quota_mode == QuotaMode.ENFORCE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quota_pause_remaining_percent": -1},
        {"quota_pause_remaining_percent": 101},
        {"quota_pause_remaining_usd": -1},
        {"quota_pause_poll_seconds": 0},
        {"quota_pause_refresh_seconds": 0},
    ],
)
def test_invalid_quota_pause_settings_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)
