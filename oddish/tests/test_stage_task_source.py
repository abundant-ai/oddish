from pathlib import Path
from oddish.worker.probe_staging import collect_task_source
from oddish.worker.probe_overlay import TASK_SOURCE_DIR_NAME, MAX_BYTES_PER_FILE


def _make_task(tmp: Path):
    (tmp / "environment").mkdir()
    (tmp / "environment" / "Dockerfile").write_text("FROM scratch")
    (tmp / "instruction.md").write_text("do the thing")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test.sh").write_text("exit 0")
    (tmp / "task.toml").write_text("[package]\n")


def test_collect_excludes_environment_and_copies_rest(tmp_path):
    _make_task(tmp_path)
    n = collect_task_source(tmp_path)
    dest = tmp_path / TASK_SOURCE_DIR_NAME
    assert (dest / "instruction.md").read_text() == "do the thing"
    assert (dest / "tests" / "test.sh").exists()
    assert (dest / "task.toml").exists()
    assert not (dest / "environment").exists()  # excluded
    assert not (dest / TASK_SOURCE_DIR_NAME).exists()  # never recurse into self
    assert n >= 3


def test_collect_skips_oversize_files(tmp_path):
    _make_task(tmp_path)
    (tmp_path / "big.bin").write_bytes(b"x" * (MAX_BYTES_PER_FILE + 1))
    collect_task_source(tmp_path)
    assert not (tmp_path / TASK_SOURCE_DIR_NAME / "big.bin").exists()
