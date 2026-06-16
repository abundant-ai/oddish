from __future__ import annotations

import os
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.api import archive_task_dir


def test_archive_task_dir_dereferences_symlinked_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ODDISH_ARCHIVE_DEREF_SYMLINKS=1, a task_path containing a relative
    symlink to a real task dir should package the real files, not a broken
    symlink."""
    monkeypatch.setenv("ODDISH_ARCHIVE_DEREF_SYMLINKS", "1")

    # Real task dir living outside the task_path.
    real_task = tmp_path / "real" / "my-task"
    real_task.mkdir(parents=True)
    expected = 'name = "my-task"\n'
    (real_task / "task.toml").write_text(expected)

    # task_path containing a *relative* symlink <slug> -> the real task dir.
    task_path = tmp_path / "experiment"
    task_path.mkdir()
    slug = "my-task"
    link = task_path / slug
    link.symlink_to(os.path.relpath(real_task, task_path))
    assert link.is_symlink()

    tarball_path = archive_task_dir(task_path)

    # The tarball member for the symlinked entry must be a real dir, not a
    # symlink, when the opt-in flag is set.
    with tarfile.open(tarball_path, "r:gz") as tar:
        member = tar.getmember(slug)
        assert not member.issym()
        assert member.isdir()

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(extract_dir, filter="data")

    extracted_toml = extract_dir / slug / "task.toml"
    # The symlink must have been dereferenced into a real regular file.
    assert extracted_toml.is_file()
    assert not extracted_toml.is_symlink()
    assert extracted_toml.read_text() == expected


def test_archive_task_dir_preserves_symlink_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default behavior (flag unset) preserves symlink semantics so uploads stay
    byte-identical to the pre-feature behavior: a symlinked entry is packaged as
    a symlink member, not dereferenced."""
    monkeypatch.delenv("ODDISH_ARCHIVE_DEREF_SYMLINKS", raising=False)

    # Real task dir living outside the task_path.
    real_task = tmp_path / "real" / "my-task"
    real_task.mkdir(parents=True)
    (real_task / "task.toml").write_text('name = "my-task"\n')

    # task_path containing a *relative* symlink <slug> -> the real task dir.
    task_path = tmp_path / "experiment"
    task_path.mkdir()
    slug = "my-task"
    link = task_path / slug
    rel_target = os.path.relpath(real_task, task_path)
    link.symlink_to(rel_target)
    assert link.is_symlink()

    tarball_path = archive_task_dir(task_path)

    # The tarball member for the symlinked entry must remain a symlink (the
    # pre-feature behavior), not a dereferenced real file/dir.
    with tarfile.open(tarball_path, "r:gz") as tar:
        member = tar.getmember(slug)
        assert member.issym()
        assert member.linkname == rel_target
        # A preserved symlink is not recursed into, so no real contents land
        # in the archive.
        assert tar.getnames() == [slug]
