from __future__ import annotations

import os
from pathlib import Path
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.api import archive_task_dir


def test_archive_task_dir_dereferences_symlinked_task(tmp_path: Path) -> None:
    """A task_path containing a relative symlink to a real task dir should
    package the real files, not a broken symlink."""
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

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(extract_dir, filter="data")

    extracted_toml = extract_dir / slug / "task.toml"
    # The symlink must have been dereferenced into a real regular file.
    assert extracted_toml.is_file()
    assert not extracted_toml.is_symlink()
    assert extracted_toml.read_text() == expected
