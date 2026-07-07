from decimal import Decimal

from oddish.config import QuotaMode, Settings


def test_quota_defaults_are_enforced_at_two_hundred_usd(monkeypatch):
    monkeypatch.delenv("ODDISH_DEFAULT_DAILY_QUOTA_USD", raising=False)
    monkeypatch.delenv("ODDISH_QUOTA_MODE", raising=False)

    settings = Settings(_env_file=None)

    assert settings.default_daily_quota_usd == Decimal("200.00")
    assert settings.quota_mode == QuotaMode.ENFORCE
