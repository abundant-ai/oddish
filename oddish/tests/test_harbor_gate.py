import pytest

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE, Settings
from oddish.core.harbor_source import (
    HarborSourceError,
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


def test_allowlisted_non_default_pin_is_resolved_and_stamped(monkeypatch):
    import oddish.core.harbor_source as hs

    monkeypatch.setattr(
        hs, "resolve_harbor_pin", lambda s, r: hs.ResolvedPin(s, "c" * 40)
    )
    hc, variant = resolve_and_gate_harbor(
        HarborConfig(source="https://github.com/dot-agi/harbor", ref="main"),
        settings=_settings(),
    )
    # Not the default and not a blessed variant -> ephemeral, stamped, executable.
    assert variant == "ephemeral"
    assert hc.resolved_sha == "c" * 40
    assert hc.variant_id == "ephemeral"
    assert hc.source == "https://github.com/dot-agi/harbor"


def test_disallowed_source_is_rejected(monkeypatch):
    import oddish.core.harbor_source as hs

    # The allowlist is the gate: a disallowed source raises before any resolve.
    monkeypatch.setattr(
        hs,
        "resolve_harbor_pin",
        lambda s, r: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    with pytest.raises(HarborSourceError):
        resolve_and_gate_harbor(
            HarborConfig(source="https://github.com/evil/harbor", ref="main"),
            settings=_settings(),
        )
