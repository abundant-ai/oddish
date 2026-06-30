"""Correctness tests for the Phase 1.1.1 task-browser filters.

Mirrors ``test_browse_search.py``: schema is built with
``Base.metadata.create_all`` on an empty Postgres (``ODDISH_DATABASE_URL``);
skips when unset. Exercises the direct task-column filters and the
trial-level ``EXISTS`` filters added to ``browse_tasks_core``.

Dataset (org1):
- alpha  COMPLETED, link set, created 2 days ago; 1 real trial
         (claude-code / modal / SUCCESS / reward 1.0 / 1.5k tokens /
         20 steps / has_trajectory / no error); member of exp-real.
- beta   RUNNING, no link, created now; 1 real trial
         (codex / docker / FAILED / reward 0.0 / 300k tokens / 200 steps /
         no trajectory / error "boom").
- gamma  COMPLETED, created now; ONLY a probe trial (gemini-cli). Probe
         trials must be invisible to every default trial filter.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import browse_task_facets_core, browse_tasks_core
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
            insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
            values ('t-a','alpha','org1','u','LOW','COMPLETED','p','http://x','{}'::jsonb,false,false,now() - interval '2 day',now()),
                   ('t-b','beta','org1','u','LOW','RUNNING','p',null,'{}'::jsonb,false,false,now(),now()),
                   ('t-c','gamma','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
            insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
            values ('v-a','t-a',1,'p',now(),now()),
                   ('v-a-old','t-a',0,'p',now(),now()),
                   ('v-b','t-b',1,'p',now(),now()),
                   ('v-c','t-c',1,'p',now(),now());
            update tasks set current_version_id='v-a' where id='t-a';
            update tasks set current_version_id='v-b' where id='t-b';
            update tasks set current_version_id='v-c' where id='t-c';
            insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
            values
              ('tr-a','tr-a','t-a','v-a','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,1000,500,0,20,true,now() - interval '1 day',1,6,0,now() - interval '1 day',now()),
              ('tr-b','tr-b','t-b','v-b','exp-real','org1','codex','openai','q',30,'docker','{}'::jsonb,'FAILED','oddish',false,0.0,'boom',200000,100000,0,200,false,now(),3,6,0,now(),now()),
              ('tr-c','tr-c','t-c','v-c','exp-probe','org1','gemini-cli','google','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',true,1.0,null,100,100,0,5,true,now(),1,6,0,now(),now()),
              ('tr-a-old','tr-a-old','t-a','v-a-old','exp-real','org1','legacy-agent','anthropic','q',30,'modal','{}'::jsonb,'success','oddish',false,1.0,null,100,50,0,10,true,now() - interval '3 day',1,6,0,now() - interval '3 day',now());
            insert into task_experiments (task_id,experiment_id,created_at)
            values ('t-a','exp-real',now()),('t-b','exp-real',now());
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))


async def _names(session, **filters):
    resp = await browse_tasks_core(session, org_id=ORG, limit=50, offset=0, **filters)
    return {item.name for item in resp.items}


async def test_browse_filters():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            # Sanity: all three tasks visible unfiltered.
            assert await _names(session) == {"alpha", "beta", "gamma"}

            # Task-column filters.
            assert await _names(session, statuses=["running"]) == {"beta"}
            assert await _names(session, has_link=True) == {"alpha"}
            assert await _names(session, experiment_ids=["exp-real"]) == {
                "alpha",
                "beta",
            }

            cutoff = datetime.now(timezone.utc) - timedelta(days=1)
            assert await _names(session, created_before=cutoff) == {"alpha"}
            assert await _names(session, created_after=cutoff) == {"beta", "gamma"}

            # Trial-level EXISTS filters.
            assert await _names(session, agents=["claude-code"]) == {"alpha"}
            assert await _names(session, agents=["codex"]) == {"beta"}
            # Agent+model pair: trials have a null model, so the token is the
            # bare agent and matches the same trial's (agent, null) pair.
            assert await _names(session, agent_models=["claude-code"]) == {
                "alpha"
            }
            assert await _names(session, agent_models=["nope"]) == set()
            assert await _names(session, environments=["docker"]) == {"beta"}
            assert await _names(session, has_trajectory=True) == {"alpha"}
            assert await _names(session, has_error=True) == {"beta"}
            assert await _names(session, min_tokens=100_000) == {"beta"}
            assert await _names(session, max_tokens=2_000) == {"alpha"}
            assert await _names(session, min_steps=100) == {"beta"}
            assert await _names(session, reward_min=1.0) == {"alpha"}
            assert await _names(session, reward_max=0.0) == {"beta"}

            # Probe trials are invisible to the default trial filters: gamma's
            # only trial is a probe, so it must never match a trial filter ...
            assert await _names(session, agents=["gemini-cli"]) == set()
            # ... but trial_is_probe opts back into them.
            assert await _names(session, trial_is_probe=True) == {"gamma"}
    finally:
        await engine.dispose()


async def test_browse_facets_scope():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            facets = await browse_task_facets_core(session, org_id=ORG)
        # Facets mirror the browse filters: only current-version, non-probe,
        # non-superseded trials. So 'gemini-cli' (probe) and 'legacy-agent'
        # (on an old, non-current version) must NOT appear.
        assert set(facets.agents) == {"claude-code", "codex"}
        pairs = {(p.agent, p.model) for p in facets.agent_models}
        assert pairs == {("claude-code", None), ("codex", None)}
    finally:
        await engine.dispose()
