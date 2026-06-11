import pytest
from oddish.core.experiment_s3 import is_within_scope


def test_key_inside_prefix_allowed():
    prefixes = ["tasks/abc/trials/abc-0/", "tasks/abc/trials/abc-1/"]
    assert is_within_scope("tasks/abc/trials/abc-0/result.json", prefixes)


def test_key_outside_prefix_rejected():
    prefixes = ["tasks/abc/trials/abc-0/"]
    assert not is_within_scope("tasks/other/trials/other-0/x", prefixes)


def test_path_traversal_rejected():
    prefixes = ["tasks/abc/trials/abc-0/"]
    assert not is_within_scope("tasks/abc/trials/abc-0/../../secret", prefixes)


def test_prefix_truncation_does_not_leak_adjacent_trial():
    # T-1 must NOT match T-10 (prefix-truncation scope escape).
    prefixes = ["tasks/abc/trials/abc-1/"]
    assert not is_within_scope("tasks/abc/trials/abc-10/result.json", prefixes)


def test_exact_prefix_dir_key_allowed():
    prefixes = ["tasks/abc/trials/abc-1/"]
    assert is_within_scope("tasks/abc/trials/abc-1/result.json", prefixes)
