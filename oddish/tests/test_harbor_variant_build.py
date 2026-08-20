"""Pure helpers for building per-variant images/Functions (Modal-side wiring)."""

from __future__ import annotations

from oddish.core.harbor_source import (
    harbor_git_requirement,
    harbor_sandbox_requirement,
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
    assert (
        harbor_variant_function_name("harbor-next") == "process_single_job__harbor-next"
    )


def test_harbor_sandbox_requirement_renders_github_commit_tarball():
    # Sandboxes have no git binary; a GitHub source must install over HTTPS.
    req = harbor_sandbox_requirement("https://github.com/dot-agi/harbor", "a" * 40)
    assert req == (
        f"harbor @ https://github.com/dot-agi/harbor/archive/{'a' * 40}.tar.gz"
    )


def test_harbor_sandbox_requirement_strips_git_prefix_and_dot_git_suffix():
    req = harbor_sandbox_requirement(
        "git+https://github.com/dot-agi/harbor.git", "b" * 40
    )
    assert req == (
        f"harbor @ https://github.com/dot-agi/harbor/archive/{'b' * 40}.tar.gz"
    )


def test_harbor_sandbox_requirement_renders_extras_group():
    req = harbor_sandbox_requirement(
        "https://github.com/dot-agi/harbor", "a" * 40, extras=["daytona"]
    )
    assert req == (
        "harbor[daytona] @ "
        f"https://github.com/dot-agi/harbor/archive/{'a' * 40}.tar.gz"
    )


def test_harbor_sandbox_requirement_non_github_falls_back_to_git_reference():
    # No forge tarball endpoint to target: only the git+ form can express
    # an arbitrary git remote, so the rendering matches harbor_git_requirement.
    src = "https://gitlab.com/dot-agi/harbor"
    req = harbor_sandbox_requirement(src, "c" * 40)
    assert req == harbor_git_requirement(src, "c" * 40)
    assert req == f"harbor @ git+{src}@{'c' * 40}"
