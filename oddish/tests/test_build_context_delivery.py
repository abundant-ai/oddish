from pathlib import Path
import pytest

from oddish.worker.probe_staging import collect_task_source, _inject_build_context_copies
from oddish.worker.probe_overlay import TASK_SOURCE_DIR_NAME, RELATED_DIR_NAME


def _task(tmp: Path, dockerfile=True):
    (tmp / "environment").mkdir()
    if dockerfile:
        (tmp / "environment" / "Dockerfile").write_text("FROM python:3.11\nWORKDIR /app\n")
    (tmp / "instruction.md").write_text("do it")
    return tmp


def test_collect_task_source_lands_in_build_context(tmp_path):
    _task(tmp_path)
    collect_task_source(tmp_path)
    assert (tmp_path / "environment" / TASK_SOURCE_DIR_NAME / "instruction.md").exists()


def test_inject_appends_copy_when_dir_staged(tmp_path):
    _task(tmp_path)
    (tmp_path / "environment" / TASK_SOURCE_DIR_NAME).mkdir()
    (tmp_path / "environment" / TASK_SOURCE_DIR_NAME / "x").write_text("y")
    _inject_build_context_copies(tmp_path)
    df = (tmp_path / "environment" / "Dockerfile").read_text()
    assert f"COPY {TASK_SOURCE_DIR_NAME} /app/{TASK_SOURCE_DIR_NAME}" in df


def test_inject_is_idempotent(tmp_path):
    _task(tmp_path)
    (tmp_path / "environment" / TASK_SOURCE_DIR_NAME).mkdir()
    (tmp_path / "environment" / TASK_SOURCE_DIR_NAME / "x").write_text("y")
    _inject_build_context_copies(tmp_path)
    _inject_build_context_copies(tmp_path)
    df = (tmp_path / "environment" / "Dockerfile").read_text()
    assert df.count(f"COPY {TASK_SOURCE_DIR_NAME} /app/{TASK_SOURCE_DIR_NAME}") == 1


def test_inject_noop_without_dockerfile(tmp_path):
    _task(tmp_path, dockerfile=False)
    (tmp_path / "environment" / TASK_SOURCE_DIR_NAME).mkdir()
    _inject_build_context_copies(tmp_path)  # must not raise
    assert not (tmp_path / "environment" / "Dockerfile").exists()


def test_inject_unignores_in_dockerignore(tmp_path):
    _task(tmp_path)
    (tmp_path / "environment" / ".dockerignore").write_text("*\n")
    (tmp_path / "environment" / RELATED_DIR_NAME).mkdir()
    (tmp_path / "environment" / RELATED_DIR_NAME / "x").write_text("y")
    _inject_build_context_copies(tmp_path)
    di = (tmp_path / "environment" / ".dockerignore").read_text()
    assert f"!{RELATED_DIR_NAME}" in di
