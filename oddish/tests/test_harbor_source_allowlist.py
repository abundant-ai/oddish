import pytest

from oddish.core.harbor_source import HarborSourceError, assert_allowed


def test_default_fork_lowercase_is_allowed():
    assert_allowed(
        "https://github.com/rishidesai/harbor",
        allowed="https://github.com/rishidesai/*",
    )


def test_allowlist_is_case_insensitive_both_sides():
    # User typed mixed-case org; allowlist is lowercase. Must still match.
    assert_allowed(
        "https://github.com/RishiDesai/Harbor",
        allowed="https://github.com/rishidesai/*",
    )
    assert_allowed(
        "https://github.com/dot-agi/harbor",
        allowed="https://GITHUB.com/DOT-AGI/*",
    )


def test_disallowed_source_raises():
    with pytest.raises(HarborSourceError):
        assert_allowed(
            "https://github.com/evil/harbor",
            allowed="https://github.com/rishidesai/*,https://github.com/dot-agi/*",
        )


def test_git_plus_prefix_is_normalized_before_match():
    assert_allowed(
        "git+https://github.com/dot-agi/harbor",
        allowed="https://github.com/dot-agi/*",
    )
