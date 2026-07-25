import re
import tomllib

from oddish.config import (
    HARBOR_DEFAULT_SHA,
    HARBOR_DEFAULT_SOURCE,
    Settings,
    parse_harbor_spec,
)

FORK = "https://github.com/abundant-ai/harbor"


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
    # The backend mirrors oddish's harbor pin; guard it too so a future re-pin
    # that updates only oddish can't silently split the two workers' harbor.
    backend_lock = open("../backend/uv.lock", encoding="utf-8").read()
    assert (
        HARBOR_DEFAULT_SHA in backend_lock
    ), "HARBOR_DEFAULT_SHA drifted from backend/uv.lock"


def test_probe_harbor_ref_matches_pyproject_pin():
    # The probe fetches harbor from ``harbor_source_ref``; it must track the same
    # branch the dependency is pinned to, or probe trials run different harbor code
    # than the trials being probed. Derived from pyproject so the two are checked
    # to move together whenever the pin is re-pointed.
    with open("pyproject.toml", "rb") as fh:
        harbor_pin = tomllib.load(fh)["tool"]["uv"]["sources"]["harbor"]
    assert Settings().harbor_source_ref == harbor_pin["branch"]


def test_pyproject_default_source_matches_config():
    # pyproject<->config drift guard: the baked default harbor source in
    # pyproject must equal HARBOR_DEFAULT_SOURCE, so the default worker image
    # bakes exactly the pin the server classifies as "default" (abundant-ai, NOT
    # harbor-gke -- that is a blessed variant on its own image).
    with open("pyproject.toml", "rb") as fh:
        harbor_pin = tomllib.load(fh)["tool"]["uv"]["sources"]["harbor"]
    assert harbor_pin["git"] == HARBOR_DEFAULT_SOURCE


def test_r1_url_with_userinfo_does_not_split_ref_on_userinfo_at():
    # A userinfo '@' (user:token@host) must NOT be treated as the ref delimiter;
    # the source URL is kept intact and the ref is empty (default-branch HEAD).
    assert parse_harbor_spec("https://user:token@github.com/dot-agi/harbor") == (
        "https://user:token@github.com/dot-agi/harbor",
        "",
    )


def test_r1_url_with_userinfo_and_trailing_ref():
    # userinfo '@' kept in source; only the trailing '@ref' in the path is split.
    assert parse_harbor_spec("https://user:token@github.com/dot-agi/harbor@abc123") == (
        "https://user:token@github.com/dot-agi/harbor",
        "abc123",
    )


def test_r1_ssh_url_with_userinfo():
    assert parse_harbor_spec("ssh://git@github.com/dot-agi/harbor") == (
        "ssh://git@github.com/dot-agi/harbor",
        "",
    )
    assert parse_harbor_spec("ssh://git@github.com/dot-agi/harbor@v2") == (
        "ssh://git@github.com/dot-agi/harbor",
        "v2",
    )


def test_r1_scp_form_git_at_host():
    # scp-style git@host:org/repo[@ref] — userinfo 'git@' before the ':' is kept.
    assert parse_harbor_spec("git@github.com:dot-agi/harbor@v2") == (
        "git@github.com:dot-agi/harbor",
        "v2",
    )
    assert parse_harbor_spec("git@github.com:dot-agi/harbor") == (
        "git@github.com:dot-agi/harbor",
        "",
    )
