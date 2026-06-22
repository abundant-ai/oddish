import re

from oddish.config import (
    HARBOR_DEFAULT_SHA,
    HARBOR_DEFAULT_SOURCE,
    parse_harbor_spec,
)

FORK = "https://github.com/rishidesai/harbor"


def test_r3_bare_ref_uses_locked_fork():
    assert parse_harbor_spec("main") == (FORK, "main")
    assert parse_harbor_spec("v0.13.1") == (FORK, "v0.13.1")
    assert parse_harbor_spec("a" * 40) == (FORK, "a" * 40)
    assert parse_harbor_spec("feature/x") == (FORK, "feature/x")
    assert parse_harbor_spec("refs/pull/123/head") == (FORK, "refs/pull/123/head")


def test_r3_bare_org_repo_without_at_is_a_fork_branch_not_a_repo():
    # The only collision: bare "org/repo" with NO "@" is a branch on the fork.
    assert parse_harbor_spec("dot-agi/harbor") == (FORK, "dot-agi/harbor")


def test_r2_org_repo_at_ref():
    assert parse_harbor_spec("dot-agi/harbor@feature/x") == (
        "https://github.com/dot-agi/harbor",
        "feature/x",
    )


def test_r1_url_with_ref_after_host():
    assert parse_harbor_spec("https://github.com/dot-agi/harbor@abc123") == (
        "https://github.com/dot-agi/harbor",
        "abc123",
    )
    assert parse_harbor_spec("git+https://github.com/dot-agi/harbor@v2") == (
        "git+https://github.com/dot-agi/harbor",
        "v2",
    )


def test_r1_url_without_ref_yields_empty_ref_for_default_branch_head():
    assert parse_harbor_spec("https://github.com/dot-agi/harbor") == (
        "https://github.com/dot-agi/harbor",
        "",
    )


def test_default_sha_matches_uv_lock_pin():
    lock = open("uv.lock", encoding="utf-8").read()
    assert HARBOR_DEFAULT_SHA in lock, "HARBOR_DEFAULT_SHA drifted from oddish/uv.lock"
    assert re.fullmatch(r"[0-9a-f]{40}", HARBOR_DEFAULT_SHA)
    assert HARBOR_DEFAULT_SOURCE == FORK
