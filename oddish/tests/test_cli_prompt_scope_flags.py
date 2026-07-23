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
