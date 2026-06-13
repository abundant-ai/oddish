"""Correctness tests for the task-browser search (`browse_tasks_core`).

Schema is built with ``Base.metadata.create_all`` on an empty Postgres
(``ODDISH_DATABASE_URL``); skips when unset. Covers:
- probe runs must not pollute browse aggregates, ordering, or chips
- user-typed LIKE wildcards (%, _, \\) are literals, not patterns
- the free-text grammar: terms AND in any order, "quoted phrase", -exclusion
"""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import browse_tasks_core
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
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))


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
        # ... must not surface as a latest-trial chip or an experiment chip
        assert old.latest_trials == []
        assert old.experiments == []

        # ... and must not drive ordering: t-old's only activity is the probe
        # (most recent event), so without the fix it sorts ABOVE t-new.
        names = [i.name for i in resp.items]
        assert names.index("newer-task") < names.index("older-task")

        new = items["newer-task"]
        assert new.total_trials == 1  # the real trial still counts
        assert [e.name for e in new.experiments] == ["Real Exp"]
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
        # unquoted words AND together in any order ("newer-task" has both)
        assert [i.name for i in multi.items] == ["newer-task"]
        # a quoted phrase matches contiguously ...
        assert [i.name for i in phrase.items] == ["match 100% done"]
        # ... so the same words out of order match nothing
        assert phrase_wrong_order.items == []
        # -term excludes
        assert [i.name for i in excluded.items] == ["newer-task"]
    finally:
        await engine.dispose()
