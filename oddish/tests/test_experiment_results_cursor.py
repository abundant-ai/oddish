"""Real-driver coverage; requires a disposable ODDISH_TEST_DATABASE_URL."""

import json
import os
from collections import Counter
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import event, insert, text

from oddish.core.cost_exclusions import load_cost_exclusions
from oddish.core.endpoints.experiment_page import stream_experiment_results
from oddish.db import connection
from oddish.db.models import (
    Base,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    task_experiments,
)


@pytest_asyncio.fixture
async def database(monkeypatch):
    url = os.environ.get("ODDISH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable ODDISH_TEST_DATABASE_URL")
    schema = "experiment_results_" + uuid4().hex
    admin = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    connect_args = connection._base_connect_args()
    connect_args["server_settings"] = {
        **connect_args["server_settings"],
        "search_path": schema,
    }
    monkeypatch.setattr(connection, "_base_connect_args", lambda: connect_args)
    monkeypatch.setattr(connection, "db_url", url)
    monkeypatch.setattr(type(connection.settings), "db_use_null_pool", False)
    monkeypatch.setattr(type(connection.settings), "db_pool_size", 1)
    monkeypatch.setattr(type(connection.settings), "db_pool_max_overflow", 0)
    engine = connection._create_engine()
    monkeypatch.setattr(connection, "engine", engine)
    monkeypatch.setattr(
        connection, "async_session_maker", connection._create_session_maker(engine)
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with connection.get_session() as session:
            session.add(
                ExperimentModel(
                    id="stream-test",
                    name="Stream test",
                    org_id="test-org",
                    is_public=True,
                    public_token="test-share",
                )
            )
            tasks = [
                TaskModel(
                    id=f"task-{i}",
                    name=f"Task {i}",
                    org_id="test-org",
                    user="private-owner",
                    task_path="s3://test/task",
                )
                for i in range(501)
            ]
            session.add_all(tasks)
            await session.flush()
            await session.execute(
                insert(task_experiments),
                [
                    {"task_id": task.id, "experiment_id": "stream-test"}
                    for task in tasks
                ],
            )
            session.add_all(
                [
                    TrialModel(
                        id=f"trial-{i}",
                        name=f"Trial {i}",
                        task_id=tasks[i % 501].id,
                        experiment_id="stream-test",
                        org_id="test-org",
                        agent="codex",
                        provider="openai",
                        queue_key="openai/gpt-5",
                        model="gpt-5",
                        status=TrialStatus.SUCCESS,
                        reward=1,
                    )
                    for i in range(1001)
                ]
            )
        async with connection.get_session() as session:
            await load_cost_exclusions(session)
        yield engine
    finally:
        await engine.dispose()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_cursor_batches_reuse_connection_across_public_member_and_disconnect(
    database,
):
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(database.sync_engine, "before_cursor_execute", observe)
    # The same physical connection must support the different result shapes.
    # AsyncSession.stream fails on unnamed prepared statements with the real
    # statement_cache_size=0 config; reusing one FETCH SQL text also confuses
    # cached column metadata when switching from member to public task rows.
    for public in (False, True, False):
        statements.clear()
        records = [
            json.loads(line)
            async for line in stream_experiment_results(
                **(
                    {"public_token": "test-share"}
                    if public
                    else {"experiment_id": "stream-test", "org_id": "test-org"}
                )
            )
        ]
        assert Counter(record["type"] for record in records) == {
            "experiment": 1,
            "task": 501,
            "trial": 1001,
            "complete": 1,
        }
        assert records[0]["experiment"]["summary"]["task_count"] == 501
        assert records[0]["experiment"]["summary"]["trial_count"] == 1001
        assert records[-1] == {"type": "complete"}
        if public:
            assert "private-owner" not in json.dumps(records)
        assert sum(sql.startswith("DECLARE") for sql in statements) == 2
        assert sum(sql.startswith("FETCH FORWARD 500") for sql in statements) == 5
        assert sum(sql.startswith("CLOSE") for sql in statements) == 2
        assert database.pool.checkedout() == 0

    for close_after in (2, 503):
        stream = stream_experiment_results(
            experiment_id="stream-test", org_id="test-org"
        )
        for _ in range(close_after):
            await anext(stream)
        await stream.aclose()
        assert database.pool.checkedout() == 0
        async with connection.get_session() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_cursors WHERE name IN "
                        "('experiment_result_tasks', 'public_experiment_result_tasks', "
                        "'experiment_result_trials')"
                    )
                )
                == 0
            )
