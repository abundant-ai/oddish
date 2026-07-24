import pytest
import typer

from oddish.cli.prompt import resolve_scope_flags


_NONE = {
    "org": False,
    "user": False,
    "task": None,
    "experiment": None,
    "trial": None,
    "global_scope": False,
}


def test_defaults_to_org_scope():
    assert resolve_scope_flags(**_NONE) == ("org", None)


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"org": True}, ("org", None)),
        ({"user": True}, ("user", None)),
        ({"global_scope": True}, ("global", None)),
        ({"task": "task_a"}, ("task", "task_a")),
        ({"experiment": "exp_a"}, ("experiment", "exp_a")),
        ({"trial": "trial_a"}, ("trial", "trial_a")),
    ],
)
def test_each_flag_selects_its_scope(override, expected):
    assert resolve_scope_flags(**{**_NONE, **override}) == expected


@pytest.mark.parametrize(
    "override",
    [
        {"org": True, "user": True},
        {"task": "task_a", "experiment": "exp_a"},
        {"global_scope": True, "org": True},
        {"org": True, "user": True, "trial": "trial_a"},
    ],
)
def test_multiple_flags_are_rejected(override):
    with pytest.raises(typer.BadParameter):
        resolve_scope_flags(**{**_NONE, **override})


# ---------------------------------------------------------------------------
# `default` parameter: reads default to global, writes still default to org
# ---------------------------------------------------------------------------


def test_no_default_arg_still_defaults_to_org():
    """Omitting `default` entirely (as `upload` does) must keep writing org."""
    assert resolve_scope_flags(**_NONE) == ("org", None)


def test_default_write_explicit_org_matches_no_flags():
    assert resolve_scope_flags(**_NONE, default="org") == ("org", None)


def test_default_read_is_global():
    assert resolve_scope_flags(**_NONE, default="global") == ("global", None)


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"org": True}, ("org", None)),
        ({"user": True}, ("user", None)),
        ({"global_scope": True}, ("global", None)),
        ({"task": "task_a"}, ("task", "task_a")),
        ({"experiment": "exp_a"}, ("experiment", "exp_a")),
        ({"trial": "trial_a"}, ("trial", "trial_a")),
    ],
)
def test_default_is_ignored_once_a_flag_is_set(override, expected):
    """An explicit flag always wins, regardless of what `default` says."""
    assert resolve_scope_flags(**{**_NONE, **override}, default="global") == expected
