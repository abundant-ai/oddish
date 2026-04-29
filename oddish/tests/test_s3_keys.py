"""Tests for ``oddish.db.s3_keys``.

Covers two write-prefix modes:
- ``""`` (prod): keys land at ``tasks/...`` / ``trials/...`` exactly as
  before the prefix was introduced.
- ``previews/pr-42/`` (preview): every key is namespaced under that
  prefix.

Pinning these shapes keeps preview-prefix injection from silently
sliding into prod paths and vice versa.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings
from oddish.db import s3_keys


PREFIXES = ["", "previews/pr-42/"]


@pytest.fixture(params=PREFIXES)
def write_prefix(request, monkeypatch):
    monkeypatch.setattr(settings, "s3_write_prefix", request.param)
    return request.param


def test_task_prefix(write_prefix):
    assert s3_keys.task_prefix("abc") == f"{write_prefix}tasks/abc/"


def test_task_version_prefix(write_prefix):
    assert s3_keys.task_version_prefix("abc", 3) == f"{write_prefix}tasks/abc/v3/"


def test_task_expanded_prefix(write_prefix):
    assert (
        s3_keys.task_expanded_prefix("abc", 3) == f"{write_prefix}tasks/abc/v3-files/"
    )


def test_task_archive_key(write_prefix):
    assert (
        s3_keys.task_archive_key("abc")
        == f"{write_prefix}tasks/abc/.oddish-task.tar.gz"
    )


def test_task_archive_key_for_version(write_prefix):
    assert (
        s3_keys.task_archive_key_for_version("abc", 3)
        == f"{write_prefix}tasks/abc/v3/.oddish-task.tar.gz"
    )


def test_task_archive_key_from_prefix_does_not_re_prefix(write_prefix):
    """A caller that already has a prefixed prefix shouldn't get it doubled."""
    given = f"{write_prefix}tasks/abc/"
    assert (
        s3_keys.task_archive_key_from_prefix(given)
        == f"{write_prefix}tasks/abc/.oddish-task.tar.gz"
    )


def test_trial_prefix_nested_under_task(write_prefix):
    # Canonical shape: trial_id is "{task_id}-{int}".
    assert (
        s3_keys.trial_prefix("mytask-3")
        == f"{write_prefix}tasks/mytask/trials/mytask-3/"
    )


def test_trial_prefix_with_dashed_task_id(write_prefix):
    # task_id itself contains dashes; rpartition on '-' picks the integer suffix.
    assert (
        s3_keys.trial_prefix("my-task-3")
        == f"{write_prefix}tasks/my-task/trials/my-task-3/"
    )


def test_trial_prefix_falls_back_when_no_int_suffix(write_prefix):
    assert s3_keys.trial_prefix("standalone") == f"{write_prefix}trials/standalone/"


def test_trial_import_archive_key(write_prefix):
    assert (
        s3_keys.trial_import_archive_key("mytask-3")
        == f"{write_prefix}tasks/mytask/trials/mytask-3/.oddish-trial-import.tar.gz"
    )


def test_object_name_constants_are_literal():
    """Constants must NOT carry the env prefix (they're appended to
    already-prefixed prefixes by the helpers above)."""
    assert s3_keys.TASK_ARCHIVE_OBJECT_NAME == ".oddish-task.tar.gz"
    assert s3_keys.EXPANDED_MANIFEST_OBJECT_NAME == ".oddish-manifest.json"
    assert s3_keys.TRIAL_IMPORT_ARCHIVE_OBJECT_NAME == ".oddish-trial-import.tar.gz"


def test_invalid_write_prefix_starts_with_slash():
    """The validator should reject a prefix that anchors at the bucket root."""
    from pydantic import ValidationError
    from oddish.config import Settings

    with pytest.raises(ValidationError):
        Settings(s3_write_prefix="/previews/pr-1/")


def test_invalid_write_prefix_missing_trailing_slash():
    from pydantic import ValidationError
    from oddish.config import Settings

    with pytest.raises(ValidationError):
        Settings(s3_write_prefix="previews/pr-1")
