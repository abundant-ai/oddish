import subprocess

import pytest

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.core import harbor_source
from oddish.core.harbor_source import (
    HarborSourceError,
    ResolvedPin,
    classify_variant,
    resolve_harbor_pin,
)


def test_40_hex_ref_short_circuits_without_git(monkeypatch):
    def boom(*a, **k):  # git must NOT be called for an immutable SHA
        raise AssertionError("git ls-remote should not run for a 40-hex ref")

    monkeypatch.setattr(harbor_source.subprocess, "run", boom)
    pin = resolve_harbor_pin(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA)
    assert pin == ResolvedPin(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA)


def test_branch_ref_resolved_via_git_ls_remote(monkeypatch):
    sha = "c" * 40

    def fake_run(cmd, **k):
        assert cmd[:2] == ["git", "ls-remote"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"{sha}\trefs/heads/main\n", stderr=""
        )

    monkeypatch.setattr(harbor_source.subprocess, "run", fake_run)
    pin = resolve_harbor_pin("https://github.com/dot-agi/harbor", "main")
    assert pin == ResolvedPin("https://github.com/dot-agi/harbor", sha)


def test_unresolvable_ref_raises(monkeypatch):
    def fake_run(cmd, **k):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(harbor_source.subprocess, "run", fake_run)
    with pytest.raises(HarborSourceError):
        resolve_harbor_pin("https://github.com/dot-agi/harbor", "nope")


def test_classify_variant_default_vs_ephemeral():
    assert classify_variant(HARBOR_DEFAULT_SOURCE, HARBOR_DEFAULT_SHA) == "default"
    assert (
        classify_variant("https://github.com/dot-agi/harbor", "c" * 40) == "ephemeral"
    )
