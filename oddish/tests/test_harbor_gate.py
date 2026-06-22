import pytest

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE, Settings
from oddish.core.harbor_source import (
    HarborOverrideDisabledError,
    resolve_and_gate_harbor,
)
from oddish.schemas import HarborConfig


def _settings(**kw):
    return Settings(
        harbor_allowed_sources="https://github.com/rishidesai/*,https://github.com/dot-agi/*",
        **kw,
    )


def test_default_pin_passes_and_is_stamped_without_network():
    hc, variant = resolve_and_gate_harbor(HarborConfig(), settings=_settings())
    assert variant == "default"
    assert hc.resolved_sha == HARBOR_DEFAULT_SHA
    assert hc.variant_id == "default"
    assert hc.source == HARBOR_DEFAULT_SOURCE


def test_non_default_pin_rejected_when_overrides_disabled(monkeypatch):
    import oddish.core.harbor_source as hs

    monkeypatch.setattr(
        hs, "resolve_harbor_pin", lambda s, r: hs.ResolvedPin(s, "c" * 40)
    )
    with pytest.raises(HarborOverrideDisabledError):
        resolve_and_gate_harbor(
            HarborConfig(source="https://github.com/dot-agi/harbor", ref="main"),
            settings=_settings(harbor_overrides_enabled=False),
        )


def test_non_default_pin_allowed_when_enabled_is_stamped(monkeypatch):
    import oddish.core.harbor_source as hs

    monkeypatch.setattr(
        hs, "resolve_harbor_pin", lambda s, r: hs.ResolvedPin(s, "c" * 40)
    )
    hc, variant = resolve_and_gate_harbor(
        HarborConfig(source="https://github.com/dot-agi/harbor", ref="main"),
        settings=_settings(harbor_overrides_enabled=True),
    )
    assert variant == "ephemeral"
    assert hc.resolved_sha == "c" * 40
    assert hc.variant_id == "ephemeral"
