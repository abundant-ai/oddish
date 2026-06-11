"""Filesystem tests for probe task-file staging (no DB/S3 needed)."""

from pathlib import Path

from oddish.worker.probe_staging import stage_full_task_files


def _make_task(root: Path) -> Path:
    task = root / "my-task"
    (task / "environment").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text("FROM scratch\n")
    (task / "tests").mkdir()
    (task / "tests" / "test.sh").write_text("echo ok\n")
    (task / "solution").mkdir()
    (task / "solution" / "patch.diff").write_text("diff\n")
    (task / "task.toml").write_text("[task]\n")
    (task / "instruction.md").write_text("do the thing\n")
    return task


def test_stage_copies_task_files_into_environment(tmp_path: Path):
    task = _make_task(tmp_path)

    assert stage_full_task_files(task) is True

    staged = task / "environment" / "task_files"
    assert (staged / "tests" / "test.sh").read_text() == "echo ok\n"
    assert (staged / "solution" / "patch.diff").exists()
    assert (staged / "task.toml").exists()
    assert (staged / "instruction.md").exists()
    # environment/ itself is not recursed into task_files/.
    assert not (staged / "environment").exists()
    assert not (staged / "Dockerfile").exists()


def test_stage_skips_hidden_entries(tmp_path: Path):
    task = _make_task(tmp_path)
    (task / ".git").mkdir()
    (task / ".git" / "config").write_text("x\n")

    stage_full_task_files(task)

    assert not (task / "environment" / "task_files" / ".git").exists()


def test_stage_returns_false_without_environment(tmp_path: Path):
    task = tmp_path / "no-env"
    task.mkdir()
    (task / "instruction.md").write_text("hi\n")

    assert stage_full_task_files(task) is False
