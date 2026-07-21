"""C-1: the zip-import path must persist task.toml metadata, same as the CLI.

``_upload_task_dir`` (oddish/core/ingest/zip_imports.py) is the server-side
twin of ``oddish.cli.api.upload_task`` and is exercised by the drag-and-drop
zip import route (a live production path). It must project ``task.toml``
into ``TaskMetadata`` and pass it through to both ``initialize_task_upload``
and ``complete_task_upload``, exactly like the CLI does.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import pytest_asyncio
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import tasks as tasks_api
from oddish.core.ingest import zip_imports
from oddish.db.models import MetadataSource, TagAssignmentModel, TagModel, TaskModel, TaskVersionModel

_TASK_NAME = "zip-import-metadata-demo"


def _write_task(task_dir: Path) -> None:
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        """\
version = "1.0"

[metadata]
category = "ml-training"
description = "Zip-imported task with real metadata."

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
cpus = 1
memory_mb = 2048
""",
        encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("Solve the task.\n", encoding="utf-8")
    (task_dir / "environment").mkdir()
    (task_dir / "environment" / "Dockerfile").write_text(
        "FROM alpine:3.20\n", encoding="utf-8"
    )
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )


class _FakeStorage:
    async def get_presigned_upload_url(self, *_args, **_kwargs) -> str:
        return "https://storage.example/upload"

    async def object_exists(self, *_args, **_kwargs) -> bool:
        return True


@pytest.fixture(autouse=True)
def _mock_storage(monkeypatch):
    monkeypatch.setattr(tasks_api, "get_storage_client", lambda: _FakeStorage())


@pytest.fixture(autouse=True)
def _skip_presigned_put(monkeypatch):
    async def _noop_put(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(zip_imports, "_put_to_presigned", _noop_put)


@pytest_asyncio.fixture(autouse=True)
async def _clean_task(session):
    await session.execute(TaskModel.__table__.delete().where(TaskModel.name == _TASK_NAME))
    await session.commit()
    yield
    await session.execute(TaskModel.__table__.delete().where(TaskModel.name == _TASK_NAME))
    await session.commit()


@pytest.mark.asyncio
async def test_zip_import_persists_task_metadata(session, tmp_path: Path):
    task_dir = tmp_path / _TASK_NAME
    _write_task(task_dir)

    result = await zip_imports._upload_task_dir(
        task_dir,
        org_id=None,
        user_id=None,
        user_name=None,
        priority=None,
        message=None,
    )

    task = (
        await session.execute(select(TaskModel).where(TaskModel.id == result.task_id))
    ).scalar_one()
    assert task.description == "Zip-imported task with real metadata."
    assert task.category == "ml-training"
    assert task.metadata_source == MetadataSource.CLIENT

    version = (
        await session.execute(
            select(TaskVersionModel).where(TaskVersionModel.task_id == result.task_id)
        )
    ).scalar_one()
    assert version.version == 1
    assert version.description_snapshot == "Zip-imported task with real metadata."
    assert version.cpus == 1
    assert version.memory_mb == 2048

    category_tags = (
        await session.execute(
            select(TagAssignmentModel)
            .join(TagModel, TagModel.id == TagAssignmentModel.tag_id)
            .where(
                TagAssignmentModel.task_id == result.task_id,
                TagAssignmentModel.state == "ACTIVE",
                TagModel.key == "category",
            )
        )
    ).scalars().all()
    assert len(category_tags) == 1
