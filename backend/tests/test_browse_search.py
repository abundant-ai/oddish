"""Correctness tests for the task-browser search (`browse_tasks_core`).

Schema is built with ``Base.metadata.create_all`` on an empty Postgres
(``ODDISH_DATABASE_URL``); skips when unset. Covers:
- probe runs must not pollute browse aggregates, ordering, or chips
- user-typed LIKE wildcards (%, _, \\) are literals, not patterns
- the free-text grammar: terms AND in any order, "quoted phrase", -exclusion
"""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import browse_tasks_core
from oddish.core.endpoints.task_detail import set_task_default_version_core
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]

ORG = "org1"


async def _setup(engine):
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        stmts = """
            insert into organizations (id,name,slug,plan,settings,is_active,created_at,updated_at)
            values ('org1','O','o','free','{}'::jsonb,true,now(),now());
            insert into experiments (id,name,org_id,is_public,created_at,updated_at)
            values ('exp-real','Real Exp','org1',false,now(),now()),
                   ('exp-probe','Probe Exp','org1',false,now(),now());
            insert into tasks (id,name,org_id,"user",priority,status,task_path,tags,run_analysis,run_probe,created_at,updated_at)
            values ('t-old','older-task','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now() - interval '2 day',now()),
                   ('t-new','newer-task','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now() - interval '1 day',now()),
                   ('t-pct','match 100% done','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now(),now()),
                   ('t-und','under_score','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now(),now());
            insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
            values ('v-old','t-old',1,'p',now(),now()),
                   ('v-new','t-new',1,'p',now(),now());
            update tasks set current_version_id='v-old' where id='t-old';
            update tasks set current_version_id='v-new' where id='t-new';
            insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,finished_at,attempts,max_attempts,heartbeat_failure_count,has_trajectory,created_at,updated_at)
            values ('tr-real','tr-real','t-new','v-new','exp-real','org1','claude','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,now() - interval '1 day',1,6,0,false,now() - interval '1 day',now()),
                   ('tr-probe','tr-probe','t-old','v-old','exp-probe','org1','claude','anthropic','q',30,'modal','{}'::jsonb,'FAILED','oddish',true,0.0,now(),1,6,0,false,now(),now());
            insert into task_experiments (task_id,experiment_id,created_at)
            values ('t-new','exp-real',now());
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await refresh_task_browse_summaries(session, ["v-old", "v-new"])
        await session.commit()


async def test_probe_runs_do_not_pollute_browse():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            resp = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)
        items = {i.name: i for i in resp.items}

        old = items["older-task"]
        # the probe FAILED run must not count as a trial or a failure ...
        assert old.total_trials == 0
        assert old.failed_trials == 0
        # ... must not surface as a latest-trial chip. Experiment chips now come
        # from task_experiments membership (all-time, matching the task-detail
        # page), not from trials; the probe-only task has no membership row.
        assert old.latest_trials == []
        assert old.experiments == []

        # ... and must not drive ordering: t-old's only activity is the probe
        # (most recent event), so without the fix it sorts ABOVE t-new.
        names = [i.name for i in resp.items]
        assert names.index("newer-task") < names.index("older-task")

        new = items["newer-task"]
        assert new.total_trials == 1  # the real trial still counts
        # surfaced via its task_experiments membership row
        assert [e.name for e in new.experiments] == ["Real Exp"]
    finally:
        await engine.dispose()


async def _setup_combine(engine):
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        stmts = """
            insert into organizations (id,name,slug,plan,settings,is_active,created_at,updated_at)
            values ('org1','O','o','free','{}'::jsonb,true,now(),now());
            insert into experiments (id,name,org_id,is_public,created_at,updated_at)
            values ('exp-a','Exp A','org1',false,now(),now()),
                   ('exp-b','Exp B','org1',false,now(),now());
            insert into tasks (id,name,org_id,"user",priority,status,task_path,tags,run_analysis,run_probe,created_at,updated_at)
            values ('t-c','combine-task','org1','u','LOW','COMPLETED','p','{}'::jsonb,false,false,now(),now());
            insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
            values ('v-c','t-c',1,'p',now(),now());
            update tasks set current_version_id='v-c' where id='t-c';
            insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,model,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,idempotency_key,finished_at,attempts,max_attempts,heartbeat_failure_count,has_trajectory,created_at,updated_at)
            values ('tr-src','tr-src','t-c','v-c','exp-a','org1','claude','sonnet','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,now(),1,6,0,false,now(),now()),
                   ('tr-combine','tr-combine','t-c','v-c','exp-b','org1','claude','sonnet','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,'combine:exp-b:tr-src',now(),1,6,0,false,now(),now());
            insert into task_experiments (task_id,experiment_id,created_at)
            values ('t-c','exp-a',now()),('t-c','exp-b',now());
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await refresh_task_browse_summaries(session, ["v-c"])
        await session.commit()


async def test_combine_copies_excluded_from_browse():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup_combine(engine)
        async with maker() as session:
            resp = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)
        item = {i.name: i for i in resp.items}["combine-task"]
        # The combine copy re-materializes the same execution under exp-b; it
        # must not double the trial count, the reward rollup, or the icon list.
        assert item.total_trials == 1
        assert item.reward_success == 1
        assert [t.id for t in item.latest_trials] == ["tr-src"]
        assert item.latest_trials[0].agent == "claude"
        assert item.latest_trials[0].model == "sonnet"
    finally:
        await engine.dispose()


async def test_default_browse_preview_is_bounded_but_totals_are_exact():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup_combine(engine)
        async with maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO trials (
                        id, name, task_id, task_version_id, experiment_id, org_id,
                        agent, model, provider, queue_key, timeout_minutes,
                        environment, harbor_config, status, origin, is_probe,
                        reward, cost_usd, finished_at, attempts, max_attempts,
                        heartbeat_failure_count, has_trajectory, created_at, updated_at
                    )
                    SELECT 'tr-extra-' || n, 'tr-extra-' || n, 't-c', 'v-c',
                           'exp-a', 'org1', 'claude', 'sonnet', 'anthropic', 'q',
                           30, 'modal', '{}'::jsonb, 'SUCCESS', 'oddish', false,
                           1.0, 1.0, NOW() + n * interval '1 second', 1, 6, 0,
                           false, NOW() + n * interval '1 second', NOW()
                    FROM generate_series(1, 30) AS n
                    """
                )
            )
            await refresh_task_browse_summaries(session, ["v-c"])
            await session.commit()
            response = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)
            standalone = await browse_tasks_core(
                session, org_id=None, limit=10, offset=0
            )

        item = response.items[0]
        assert item.total_trials == 31
        assert item.completed_trials == 31
        assert item.pass_count == 31
        assert item.cost_usd == 30.0
        assert item.cost_trial_count == 30
        assert len(item.latest_trials) == 24
        assert item.latest_trials_truncated is True
        standalone_item = standalone.items[0]
        assert standalone_item.total_trials == item.total_trials
        assert standalone_item.cost_usd == item.cost_usd
        assert [trial.id for trial in standalone_item.latest_trials] == [
            trial.id for trial in item.latest_trials
        ]
    finally:
        await engine.dispose()


async def test_summary_refresh_serializes_concurrent_settlements():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup_combine(engine)
        async with maker() as first, maker() as second:
            insert_trial = text(
                """
                INSERT INTO trials (
                    id, name, task_id, task_version_id, experiment_id, org_id,
                    agent, model, provider, queue_key, timeout_minutes,
                    environment, harbor_config, status, origin, is_probe,
                    reward, finished_at, attempts, max_attempts,
                    heartbeat_failure_count, has_trajectory, created_at, updated_at
                ) VALUES (
                    :trial_id, :trial_id, 't-c', 'v-c', 'exp-a', 'org1',
                    'claude', 'sonnet', 'anthropic', 'q', 30, 'modal',
                    '{}'::jsonb, 'SUCCESS', 'oddish', false, 1, NOW(),
                    1, 6, 0, false, NOW(), NOW()
                )
                """
            )
            # Both inserts hold a KEY SHARE FK lock on v-c. Summary
            # serialization must not try to upgrade either lock to FOR UPDATE.
            await first.execute(insert_trial, {"trial_id": "tr-concurrent-a"})
            await second.execute(insert_trial, {"trial_id": "tr-concurrent-b"})
            await asyncio.wait_for(
                refresh_task_browse_summaries(first, ["v-c"]), timeout=0.5
            )

            second_refresh = asyncio.create_task(
                refresh_task_browse_summaries(second, ["v-c"])
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(second_refresh), timeout=0.1)

            await first.commit()
            await second_refresh
            await second.commit()

        async with maker() as check:
            row = (
                await check.execute(
                    text(
                        """
                        SELECT total_trials, completed_trials
                        FROM task_version_browse_summaries
                        WHERE task_version_id = 'v-c'
                        """
                    )
                )
            ).one()
        assert row == (3, 3)
    finally:
        await engine.dispose()


async def test_selected_default_version_changes_card_immediately():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup_combine(engine)
        async with maker() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO task_versions (
                        id, task_id, version, task_path, created_at, updated_at
                    ) VALUES ('v-c-2', 't-c', 2, 'p2', NOW(), NOW())
                    """
                )
            )
            await session.execute(
                text(
                    """
                    INSERT INTO trials (
                        id, name, task_id, task_version_id, experiment_id, org_id,
                        agent, model, provider, queue_key, timeout_minutes,
                        environment, harbor_config, status, origin, is_probe,
                        reward, finished_at, attempts, max_attempts,
                        heartbeat_failure_count, has_trajectory, created_at, updated_at
                    ) VALUES (
                        'tr-v2', 'tr-v2', 't-c', 'v-c-2', 'exp-a', 'org1',
                        'claude', 'sonnet', 'anthropic', 'q', 30, 'modal',
                        '{}'::jsonb, 'SUCCESS', 'oddish', false, 0.0, NOW(),
                        1, 6, 0, false, NOW(), NOW()
                    )
                    """
                )
            )
            await set_task_default_version_core(
                session, task_id="t-c", version=2, org_id=ORG
            )
            response = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)

        item = response.items[0]
        assert item.current_version == 2
        assert item.current_version_id == "v-c-2"
        assert item.version_count == 2
        assert item.total_trials == 1
        assert [trial.id for trial in item.latest_trials] == ["tr-v2"]
    finally:
        await engine.dispose()


async def test_search_wildcards_are_literals():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            underscore = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="_"
            )
            percent = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="100%"
            )
            plain = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="task"
            )
        # "_" must match only the name containing a literal underscore,
        # not act as a single-char wildcard matching every task.
        assert [i.name for i in underscore.items] == ["under_score"]
        # "100%" must match the literal percent name only.
        assert [i.name for i in percent.items] == ["match 100% done"]
        assert {i.name for i in plain.items} == {"older-task", "newer-task"}
    finally:
        await engine.dispose()


async def test_search_grammar_and_or_exclude():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            multi = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="task newer"
            )
            phrase = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query='"100% done"'
            )
            phrase_wrong_order = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query='"done 100%"'
            )
            excluded = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="task -older"
            )
            either = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="older OR newer"
            )
            not_kw = await browse_tasks_core(
                session, org_id=ORG, limit=10, offset=0, query="task NOT older"
            )
        # unquoted words AND together in any order ("newer-task" has both)
        assert [i.name for i in multi.items] == ["newer-task"]
        # a quoted phrase matches contiguously ...
        assert [i.name for i in phrase.items] == ["match 100% done"]
        # ... so the same words out of order match nothing
        assert phrase_wrong_order.items == []
        # -term excludes
        assert [i.name for i in excluded.items] == ["newer-task"]
        # uppercase OR matches either side
        assert {i.name for i in either.items} == {"older-task", "newer-task"}
        # uppercase NOT excludes the next term
        assert [i.name for i in not_kw.items] == ["newer-task"]
    finally:
        await engine.dispose()
