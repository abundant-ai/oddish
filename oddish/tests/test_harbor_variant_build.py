"""Pure helpers for building per-variant images/Functions (Modal-side wiring)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from oddish.core.harbor_source import (
    harbor_git_requirement,
    harbor_uv_source_rewrite_command,
    harbor_variant_function_name,
)


def test_harbor_git_requirement_is_pep508_direct_reference():
    req = harbor_git_requirement("https://github.com/dot-agi/harbor", "a" * 40)
    assert req == f"harbor @ git+https://github.com/dot-agi/harbor@{'a' * 40}"


def test_harbor_git_requirement_does_not_double_git_prefix():
    # A source that already starts with git+ must not become git+git+...
    req = harbor_git_requirement("git+https://github.com/dot-agi/harbor", "b" * 40)
    assert req == f"harbor @ git+https://github.com/dot-agi/harbor@{'b' * 40}"
    assert "git+git+" not in req


def test_harbor_git_requirement_renders_extras_group():
    # Cloud-provider SDKs are optional Harbor extras, so the override install
    # must be able to request them as a PEP 508 extras group.
    req = harbor_git_requirement(
        "https://github.com/dot-agi/harbor", "a" * 40, extras=["daytona"]
    )
    assert req == f"harbor[daytona] @ git+https://github.com/dot-agi/harbor@{'a' * 40}"


def test_harbor_git_requirement_extras_sorted_and_deduped():
    req = harbor_git_requirement(
        "https://github.com/dot-agi/harbor",
        "a" * 40,
        extras=["modal", "daytona", "modal", " ", ""],
    )
    assert req.startswith("harbor[daytona,modal] @ git+")


def test_harbor_git_requirement_empty_extras_is_bare_package():
    # Falsy / all-blank extras must not emit an empty bracket group.
    for extras in (None, [], ["", "  "]):
        req = harbor_git_requirement(
            "https://github.com/dot-agi/harbor", "a" * 40, extras=extras
        )
        assert req == f"harbor @ git+https://github.com/dot-agi/harbor@{'a' * 40}"


def test_harbor_variant_function_name():
    assert harbor_variant_function_name("harbor-next") == "process_single_job__harbor-next"


def test_uv_source_rewrite_repoints_harbor_pin_in_pyproject():
    # The command must repoint the [tool.uv.sources] harbor entry at the
    # variant's commit so uv_sync resolves the whole dep set against it.
    sample = (
        "[tool.uv.sources]\n"
        'oddish = { path = "../oddish", editable = true }\n'
        'harbor = { git = "https://github.com/rishidesai/harbor", branch = "main" }\n'
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "pyproject.toml"
        p.write_text(sample)
        cmd = harbor_uv_source_rewrite_command(
            "https://github.com/dot-agi/harbor", "c" * 40, str(p)
        )
        subprocess.run(cmd, shell=True, check=True)
        out = p.read_text()
    assert f'harbor = {{ git = "https://github.com/dot-agi/harbor", rev = "{"c" * 40}" }}' in out
    assert "rishidesai" not in out
    # The unrelated oddish source line is untouched.
    assert 'oddish = { path = "../oddish", editable = true }' in out
