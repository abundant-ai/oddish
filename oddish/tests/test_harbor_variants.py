"""Variant registry + normalized classification + source canonicalization."""

from __future__ import annotations

import subprocess

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.core import harbor_source
from oddish.core.harbor_source import (
    HarborVariant,
    ResolvedPin,
    classify_variant,
    resolve_harbor_pin,
)


def test_harbor_variant_dataclass_fields():
    v = HarborVariant(variant_id="harbor-v12", source=HARBOR_DEFAULT_SOURCE, sha="a" * 40)
    assert v.variant_id == "harbor-v12"
    assert v.source == HARBOR_DEFAULT_SOURCE
    assert v.sha == "a" * 40


def test_classify_default_normalizes_git_prefix_and_case():
    # A git+ / mixed-case spelling of the locked default must still be "default",
    # not misrouted to "ephemeral".
    assert classify_variant(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA) == "default"
    assert (
        classify_variant(f"git+{HARBOR_DEFAULT_SOURCE}", HARBOR_DEFAULT_SHA) == "default"
    )
    assert (
        classify_variant(
            "https://github.com/RishiDesai/Harbor", HARBOR_DEFAULT_SHA
        )
        == "default"
    )


def test_classify_blessed_variant_matches_normalized(monkeypatch):
    sha = "c" * 40
    monkeypatch.setattr(
        harbor_source,
        "HARBOR_VARIANTS",
        {
            "harbor-next": HarborVariant(
                variant_id="harbor-next",
                source="https://github.com/dot-agi/harbor",
                sha=sha,
            )
        },
    )
    # exact, git+, and case variants all classify to the blessed id.
    assert classify_variant("https://github.com/dot-agi/harbor", sha) == "harbor-next"
    assert classify_variant("git+https://github.com/dot-agi/harbor", sha) == "harbor-next"
    assert classify_variant("https://github.com/DOT-AGI/harbor", sha) == "harbor-next"
    # a different sha on the same source is still ephemeral.
    assert classify_variant("https://github.com/dot-agi/harbor", "d" * 40) == "ephemeral"


def test_classify_unknown_is_ephemeral():
    assert classify_variant("https://github.com/dot-agi/harbor", "e" * 40) == "ephemeral"


def test_resolve_strips_git_prefix_on_40hex_short_circuit(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git ls-remote should not run for a 40-hex ref")

    monkeypatch.setattr(harbor_source.subprocess, "run", boom)
    pin = resolve_harbor_pin(f"git+{HARBOR_DEFAULT_SOURCE}", HARBOR_DEFAULT_SHA)
    assert pin == ResolvedPin(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA)


def test_resolve_strips_git_prefix_via_ls_remote(monkeypatch):
    sha = "f" * 40

    def fake_run(cmd, **k):
        # the git+ prefix must be stripped before git sees the URL.
        assert "git+https://github.com/dot-agi/harbor" not in cmd
        assert "https://github.com/dot-agi/harbor" in cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"{sha}\trefs/heads/main\n", stderr=""
        )

    monkeypatch.setattr(harbor_source.subprocess, "run", fake_run)
    pin = resolve_harbor_pin("git+https://github.com/dot-agi/harbor", "main")
    assert pin == ResolvedPin("https://github.com/dot-agi/harbor", sha)
