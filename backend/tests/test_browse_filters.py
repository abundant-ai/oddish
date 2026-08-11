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

# Task-browser filters (Phase 1.1.1 → 2.3, no migration).

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import (
    browse_experiment_options_core,
    browse_task_facets_core,
    browse_tasks_core,
)
from oddish.core.task_browse_rollup import project_task_browse_trials
from oddish.core.trial_facets import rebuild_trial_facets_core
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]

ORG = "org1"


async def _project_tasks(session, task_ids):
    rows = await session.execute(
        text("select id from trials where task_id = any(:task_ids)"),
        {"task_ids": list(task_ids)},
    )
    await project_task_browse_trials(session, [str(row[0]) for row in rows])


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
              ('tr-a-old','tr-a-old','t-a','v-a-old','exp-real','org1','legacy-agent','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now() - interval '3 day',1,6,0,now() - interval '3 day',now());
            insert into task_experiments (task_id,experiment_id,created_at)
            values ('t-a','exp-real',now()),('t-b','exp-real',now());
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _project_tasks(session, ["t-a", "t-b", "t-c"])
        await session.commit()


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
            assert await _names(session, agent_models=["claude-code"]) == {"alpha"}
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
            # Facets read the trial_facets vocabulary; the rebuild derives it
            # from the same trial population the browse filters match.
            await rebuild_trial_facets_core(session)
            facets = await browse_task_facets_core(session, org_id=ORG)
        # Only current-version, non-probe, non-superseded trials contribute.
        # So 'gemini-cli' (probe) and 'legacy-agent' (on an old, non-current
        # version) must NOT appear.
        assert set(facets.agents) == {"claude-code", "codex"}
        pairs = {(p.agent, p.model) for p in facets.agent_models}
        assert pairs == {("claude-code", None), ("codex", None)}
        # The experiments facet is deprecated and must stay empty even though
        # org1 has experiments — serializing all of them (7.7MB at 126k) is
        # the payload regression this pins. Options come from
        # browse_experiment_options_core instead.
        assert facets.experiments == []
    finally:
        await engine.dispose()


async def _insert_option_orgs(engine):
    """Second org, a soft-deleted org1 experiment, and 205 bulk org1
    experiments so the 200-row cap is exercisable."""
    stmts = """
        insert into organizations (id,name,slug,plan,settings,is_active,created_at,updated_at)
        values ('org2','O2','o2','free','{}'::jsonb,true,now(),now());
        insert into experiments (id,name,org_id,is_public,created_at,updated_at)
        values ('exp-org2','Org Two Exp','org2',false,now(),now()),
               ('exp-del','Deleted Exp','org1',false,now(),now());
        update experiments set deleted_at=now() where id='exp-del';
        insert into experiments (id,name,org_id,is_public,created_at,updated_at)
        select 'exp-bulk-'||lpad(g::text,3,'0'), 'Bulk '||lpad(g::text,3,'0'),
               'org1', false, now(), now()
        from generate_series(1,205) as g;
    """
    async with engine.begin() as c:
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))


async def test_experiment_options():
    """Org-scoped typeahead that replaces the retired experiments facet."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_option_orgs(engine)
        async with maker() as session:
            opts = browse_experiment_options_core  # keep call sites short

            # Live org rows only, name-ordered, capped at 200 (207 live org1
            # rows exist). Soft-deleted rows must not appear.
            resp = await opts(session, org_id=ORG, limit=999)
            names = [o.name for o in resp.items]
            assert len(names) == 200
            assert names == sorted(names)
            assert "Deleted Exp" not in names
            assert "Org Two Exp" not in names

            resp = await opts(session, org_id=ORG)
            assert len(resp.items) == 50  # default page size
            resp = await opts(session, org_id=ORG, limit=1)
            assert len(resp.items) == 1

            # Case-insensitive substring search.
            resp = await opts(session, org_id=ORG, query="real")
            assert [o.id for o in resp.items] == ["exp-real"]

            # ILIKE metacharacters in user input are literals, not wildcards.
            resp = await opts(session, org_id=ORG, query="%")
            assert resp.items == []
            resp = await opts(session, org_id=ORG, query="_")
            assert resp.items == []

            # ids= hydration returns named rows and wins over query.
            resp = await opts(session, org_id=ORG, ids=["exp-real"], query="probe")
            assert [(o.id, o.name) for o in resp.items] == [("exp-real", "Real Exp")]

            # Hydration is a keyed lookup, not a paged search: every id
            # resolves even past the default search page size (a restored
            # selection of 60 chips must not truncate to 50 names) ...
            bulk_ids = [f"exp-bulk-{i:03d}" for i in range(1, 61)]
            resp = await opts(session, org_id=ORG, ids=bulk_ids)
            assert len(resp.items) == 60
            # ... duplicate ids don't consume the input cap ...
            resp = await opts(
                session, org_id=ORG, ids=["exp-real"] * 250 + ["exp-probe"]
            )
            assert {o.id for o in resp.items} == {"exp-real", "exp-probe"}
            # ... and the only truncation point is the documented input cap.
            all_ids = [f"exp-bulk-{i:03d}" for i in range(1, 206)] + [
                "exp-real",
                "exp-probe",
            ]
            resp = await opts(session, org_id=ORG, ids=all_ids)
            assert len(resp.items) == 200

            # The critical isolation property: another org's experiments are
            # invisible both by search and by id hydration.
            resp = await opts(session, org_id=ORG, query="Org Two")
            assert resp.items == []
            resp = await opts(session, org_id=ORG, ids=["exp-org2"])
            assert resp.items == []
            resp = await opts(session, org_id="org2")
            assert [o.id for o in resp.items] == ["exp-org2"]
    finally:
        await engine.dispose()


async def _insert_mixed_trial_task(engine):
    """Add task 'delta' whose two current-version real trials are MIXED:
    one errored + one clean, one with a trajectory + one without."""
    stmts = """
        insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
        values ('t-d','delta','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
        insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
        values ('v-d','t-d',1,'p',now(),now());
        update tasks set current_version_id='v-d' where id='t-d';
        insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
        values
          ('tr-d1','tr-d1','t-d','v-d','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.5,'kaboom',1000,500,0,20,true,now(),1,6,0,now(),now()),
          ('tr-d2','tr-d2','t-d','v-d','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'FAILED','oddish',false,0.5,null,1000,500,0,20,false,now(),1,6,0,now(),now());
    """
    async with engine.begin() as c:
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))


async def _insert_aggregate_tasks(engine):
    """Add tasks with KNOWN aggregate metrics for the Phase 1.2-lite filters.

    epsilon: 3 SUCCESS trials reward 1.0/0.5/0.0 -> avg 50%, tokens 6000,
             pass/partial/fail = 1/1/1, runtime 60+120+30 = 210s (avg 70s).
    zeta:    FAILED+error (harness, NULL reward) + SUCCESS reward 1.0 (pass) ->
             avg 100% (NULL reward ignored by AVG), harness/pass = 1/1, tokens
             2000, runtime 10+20 = 30s.
    eta:     two FAILED AgentTimeout trials — one WITH reward 0.5 (scores as
             'partial' per the carve-out) and one WITHOUT reward ('harness'),
             tokens 200, runtime 5s. Guards that the SQL buckets mirror
             getMatrixStatus EXACTLY (a FAILED-but-rewarded timeout is partial,
             not harness).
    """
    stmts = """
        insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
        values ('t-e','epsilon','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-z','zeta','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-h','eta','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
        insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
        values ('v-e','t-e',1,'p',now(),now()),
               ('v-z','t-z',1,'p',now(),now()),
               ('v-h','t-h',1,'p',now(),now());
        update tasks set current_version_id='v-e' where id='t-e';
        update tasks set current_version_id='v-z' where id='t-z';
        update tasks set current_version_id='v-h' where id='t-h';
        insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,started_at,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
        values
          ('te1','te1','t-e','v-e','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,1000,0,0,10,true,now() - interval '60 second',now(),1,6,0,now(),now()),
          ('te2','te2','t-e','v-e','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.5,null,2000,0,0,10,true,now() - interval '120 second',now(),1,6,0,now(),now()),
          ('te3','te3','t-e','v-e','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.0,null,3000,0,0,10,true,now() - interval '30 second',now(),1,6,0,now(),now()),
          ('tz1','tz1','t-z','v-z','exp-real','org1','codex','openai','q',30,'docker','{}'::jsonb,'FAILED','oddish',false,null,'boom',500,0,0,5,false,now() - interval '10 second',now(),1,6,0,now(),now()),
          ('tz2','tz2','t-z','v-z','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,1500,0,0,10,true,now() - interval '20 second',now(),1,6,0,now(),now()),
          ('th1','th1','t-h','v-h','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'FAILED','oddish',false,0.5,'AgentTimeoutError: timed out',100,0,0,5,false,now() - interval '5 second',now(),1,6,0,now(),now()),
          ('th2','th2','t-h','v-h','exp-real','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'FAILED','oddish',false,null,'AgentTimeoutError: timed out',100,0,0,5,false,null,null,1,6,0,now(),now());
    """
    async with engine.begin() as c:
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _project_tasks(session, ["t-e", "t-z", "t-h"])
        await session.commit()


async def test_browse_aggregate_filters():
    """Phase 1.2-lite: HAVING-equivalent aggregate filters over the scoped
    (current-version, non-probe, non-superseded) trial set."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_aggregate_tasks(engine)
        async with maker() as session:
            # Avg score is a PERCENT (0-100). alpha & zeta = 100%, epsilon &
            # eta = 50%, beta = 0%, gamma = NULL (probe-only -> excluded).
            assert await _names(session, avg_score_min=90) == {"alpha", "zeta"}
            assert await _names(session, avg_score_max=10) == {"beta"}
            assert await _names(session, avg_score_min=40, avg_score_max=60) == {
                "epsilon",
                "eta",
            }

            # Total tokens (input+output+cache summed across the task).
            assert await _names(session, total_tokens_min=250_000) == {"beta"}
            assert await _names(session, total_tokens_max=250) == {"eta"}

            # Trial counts.
            assert await _names(session, total_trials_min=3) == {"epsilon"}
            assert await _names(session, completed_trials_min=3) == {"epsilon"}
            assert await _names(session, failed_trials_min=1) == {
                "beta",
                "zeta",
                "eta",
            }

            # Bucket counts — mirror getMatrixStatus.
            assert await _names(session, pass_count_min=1) == {
                "alpha",
                "zeta",
                "epsilon",
            }
            assert await _names(session, fail_count_min=1) == {"epsilon"}
            assert await _names(session, harness_count_min=1) == {
                "beta",
                "zeta",
                "eta",
            }
            # AgentTimeout carve-out: eta's FAILED-but-rewarded timeout trial
            # scores as 'partial' (NOT harness), exactly like the card chip.
            assert await _names(session, partial_count_min=1) == {
                "epsilon",
                "eta",
            }

            # Runtime (wall-clock seconds; only trials with both timestamps).
            # alpha/beta have NULL started_at -> NULL runtime -> excluded.
            assert await _names(session, runtime_total_min=200) == {"epsilon"}
            assert await _names(session, runtime_avg_min=60) == {"epsilon"}
            assert await _names(session, runtime_total_max=10) == {"eta"}
    finally:
        await engine.dispose()


async def test_browse_aggregate_sort():
    """Phase 1.2-lite: aggregate ORDER BY (avg score desc), NULL metric last."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_aggregate_tasks(engine)
        async with maker() as session:
            resp = await browse_tasks_core(
                session, org_id=ORG, limit=50, offset=0, sort="avg_score_desc"
            )
            names = [item.name for item in resp.items]
        # 100% (alpha/zeta) before 50% (epsilon/eta) before 0% (beta); the
        # probe-only NULL-metric task (gamma) sorts last (nulls_last).
        assert names[-1] == "gamma"
        assert names.index("alpha") < names.index("epsilon")
        assert names.index("epsilon") < names.index("beta")
    finally:
        await engine.dispose()


async def _insert_compare_tasks(engine):
    """Add tasks with two subjects (agents/models) per task for the Phase 2.1
    'A beats B' comparison.

    cmp-reward:  claude-code best reward 0.9 vs codex 0.4  (A beats B on reward).
    cmp-runtime: claude-code 100s vs codex 20s             (codex beats on runtime).
    cmp-onlyA:   ONLY claude-code (reward 0.9)             (excluded — needs both).
    cmp-margin:  claude-code 0.55 vs codex 0.50            (A beats by exactly 10%).
    cmp-avg:     claude-code [1.0, 0.0] vs codex [0.6,0.6] (best: A wins; avg: B wins).
    cmp-model:   model m-a reward 0.9 vs model m-b 0.3     (model-vs-model).
    """
    stmts = """
        insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
        values ('t-cr','cmp-reward','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-cru','cmp-runtime','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-ca','cmp-onlyA','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-cm','cmp-margin','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-cavg','cmp-avg','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
               ('t-cmodel','cmp-model','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
        insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
        values ('v-cr','t-cr',1,'p',now(),now()),
               ('v-cru','t-cru',1,'p',now(),now()),
               ('v-ca','t-ca',1,'p',now(),now()),
               ('v-cm','t-cm',1,'p',now(),now()),
               ('v-cavg','t-cavg',1,'p',now(),now()),
               ('v-cmodel','t-cmodel',1,'p',now(),now());
        update tasks set current_version_id='v-cr' where id='t-cr';
        update tasks set current_version_id='v-cru' where id='t-cru';
        update tasks set current_version_id='v-ca' where id='t-ca';
        update tasks set current_version_id='v-cm' where id='t-cm';
        update tasks set current_version_id='v-cavg' where id='t-cavg';
        update tasks set current_version_id='v-cmodel' where id='t-cmodel';
        insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,model,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,started_at,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
        values
          ('cr1','cr1','t-cr','v-cr','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.9,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cr2','cr2','t-cr','v-cr','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.4,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cru1','cru1','t-cru','v-cru','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.5,null,100,50,0,10,true,now() - interval '100 second',now(),1,6,0,now(),now()),
          ('cru2','cru2','t-cru','v-cru','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.5,null,100,50,0,10,true,now() - interval '20 second',now(),1,6,0,now(),now()),
          ('ca1','ca1','t-ca','v-ca','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.9,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cm1','cm1','t-cm','v-cm','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.55,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cm2','cm2','t-cm','v-cm','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.50,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cavg1','cavg1','t-cavg','v-cavg','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cavg2','cavg2','t-cavg','v-cavg','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.0,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cavg3','cavg3','t-cavg','v-cavg','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.6,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cavg4','cavg4','t-cavg','v-cavg','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.6,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmod1','cmod1','t-cmodel','v-cmodel','exp-real','org1','claude-code','m-a','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.9,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmod2','cmod2','t-cmodel','v-cmodel','exp-real','org1','claude-code','m-b','anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.3,null,100,50,0,10,true,null,null,1,6,0,now(),now());
    """
    async with engine.begin() as c:
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _project_tasks(
            session,
            ["t-cr", "t-cru", "t-ca", "t-cm", "t-cavg", "t-cmodel"],
        )
        await session.commit()


async def _cmp(session, by, a, b, metric, agg, margin=None, unit=None):
    return await _names(
        session,
        compare_by=by,
        compare_a=a,
        compare_b=b,
        compare_metric=metric,
        compare_agg=agg,
        compare_margin=margin,
        compare_margin_unit=unit,
    )


async def test_browse_agent_compare():
    """Phase 2.1: 'A beats B' over the scoped trials — reward/runtime, best/avg,
    margins (% and abs), model-vs-model, and missing-subject exclusion."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_compare_tasks(engine)
        async with maker() as session:
            # Reward, best. cmp-onlyA is excluded (only one of the two agents);
            # cmp-runtime ties on reward; cmp-model has no codex trials.
            assert await _cmp(
                session, "agent", "claude-code", "codex", "reward", "best"
            ) == {"cmp-reward", "cmp-margin", "cmp-avg"}

            # Aggregation flips cmp-avg: A's best (1.0) beats B, but A's average
            # (0.5) loses to B's average (0.6).
            assert await _cmp(
                session, "agent", "claude-code", "codex", "reward", "avg"
            ) == {"cmp-reward", "cmp-margin"}

            # Run time is lower-better: codex (20s) beats claude-code (100s);
            # the reverse direction matches nothing.
            assert await _cmp(
                session, "agent", "codex", "claude-code", "runtime", "best"
            ) == {"cmp-runtime"}
            assert (
                await _cmp(session, "agent", "claude-code", "codex", "runtime", "best")
                == set()
            )

            # Margin (percent of B). cmp-margin is exactly 10% higher, so it
            # passes >5% but not >10% (strictly greater).
            assert await _cmp(
                session, "agent", "claude-code", "codex", "reward", "best", 5, "pct"
            ) == {"cmp-reward", "cmp-margin", "cmp-avg"}
            assert await _cmp(
                session, "agent", "claude-code", "codex", "reward", "best", 10, "pct"
            ) == {"cmp-reward", "cmp-avg"}

            # Margin (absolute): A must beat B by > 0.2 reward.
            assert await _cmp(
                session, "agent", "claude-code", "codex", "reward", "best", 0.2, "abs"
            ) == {"cmp-reward", "cmp-avg"}

            # Model-vs-model on the same control.
            assert await _cmp(session, "model", "m-a", "m-b", "reward", "best") == {
                "cmp-model"
            }
    finally:
        await engine.dispose()


async def test_browse_or_groups():
    """Phase 2.2 'Match any of…': OR of AND-groups, ANDed with the flat filters.

    Base dataset (from ``_setup``): alpha = claude-code, link set, avg 100%;
    beta = codex, errored, avg 0%; gamma = probe-only (gemini-cli).
    """
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            # A single group is just the equivalent AND filter.
            assert await _names(session, or_groups=[{"agents": ["claude-code"]}]) == {
                "alpha"
            }
            # Two groups ORed.
            assert await _names(
                session,
                or_groups=[{"agents": ["claude-code"]}, {"agents": ["codex"]}],
            ) == {"alpha", "beta"}
            # AND inside a group.
            assert await _names(
                session, or_groups=[{"agents": ["codex"], "has_error": True}]
            ) == {"beta"}
            assert (
                await _names(
                    session,
                    or_groups=[{"agents": ["claude-code"], "has_error": True}],
                )
                == set()
            )
            # Cross-field OR — the capability the block exists for:
            # (claude-code AND has_link) OR (codex AND has_error).
            assert await _names(
                session,
                or_groups=[
                    {"agents": ["claude-code"], "has_link": True},
                    {"agents": ["codex"], "has_error": True},
                ],
            ) == {"alpha", "beta"}
            # Aggregate condition inside a group forces the metrics join.
            assert await _names(session, or_groups=[{"avg_score_min": 90}]) == {"alpha"}
            assert await _names(
                session,
                or_groups=[{"avg_score_min": 90}, {"avg_score_max": 10}],
            ) == {"alpha", "beta"}
            # Flat filters AND on top of the OR block: has_link=True narrows the
            # (claude-code OR codex) union to just alpha.
            assert await _names(
                session,
                has_link=True,
                or_groups=[{"agents": ["claude-code"]}, {"agents": ["codex"]}],
            ) == {"alpha"}
            # Probe-only task stays excluded (same scope as the flat trial filters).
            assert (
                await _names(session, or_groups=[{"agents": ["gemini-cli"]}]) == set()
            )
            # Empty group / empty list are no-ops.
            assert await _names(session, or_groups=[{}]) == {
                "alpha",
                "beta",
                "gamma",
            }
            assert await _names(session, or_groups=[]) == {"alpha", "beta", "gamma"}
    finally:
        await engine.dispose()


async def _insert_median_task(engine):
    """Add 'cmp-median' where mean and median diverge: claude-code reward
    [1.0, 1.0, 0.0] (avg 0.667, median 1.0) vs codex [0.8, 0.8] (0.8). So
    claude-code beats codex on best & median but not on average."""
    stmts = """
        insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
        values ('t-cmed','cmp-median','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
        insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
        values ('v-cmed','t-cmed',1,'p',now(),now());
        update tasks set current_version_id='v-cmed' where id='t-cmed';
        insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,model,provider,queue_key,timeout_minutes,environment,harbor_config,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,started_at,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
        values
          ('cmd1','cmd1','t-cmed','v-cmed','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmd2','cmd2','t-cmed','v-cmed','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmd3','cmd3','t-cmed','v-cmed','exp-real','org1','claude-code',null,'anthropic','q',30,'modal','{}'::jsonb,'SUCCESS','oddish',false,0.0,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmd4','cmd4','t-cmed','v-cmed','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.8,null,100,50,0,10,true,null,null,1,6,0,now(),now()),
          ('cmd5','cmd5','t-cmed','v-cmed','exp-real','org1','codex',null,'openai','q',30,'docker','{}'::jsonb,'SUCCESS','oddish',false,0.8,null,100,50,0,10,true,null,null,1,6,0,now(),now());
    """
    async with engine.begin() as c:
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _project_tasks(session, ["t-cmed"])
        await session.commit()


async def test_browse_comparison_extras():
    """Phase 2.3: top performer, compare-in-groups, pass-rate filter, median."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_compare_tasks(engine)
        await _insert_median_task(engine)
        async with maker() as session:
            # --- Top performer (argmax / argmin across all subjects) ---
            # codex is the reward top only where it's the sole agent (beta) or
            # ties for best (cmp-runtime: 0.5 vs 0.5).
            assert await _names(
                session, top_by="agent", top_value="codex", top_metric="reward"
            ) == {"beta", "cmp-runtime"}
            # On run time (lower-better) codex wins cmp-runtime (20s vs 100s).
            assert await _names(
                session, top_by="agent", top_value="codex", top_metric="runtime"
            ) == {"cmp-runtime"}
            # Model-vs-field: m-a is the top model on reward only in cmp-model.
            assert await _names(
                session, top_by="model", top_value="m-a", top_metric="reward"
            ) == {"cmp-model"}

            # --- Median vs best vs avg (global compare) ---
            cc_beats_codex = dict(
                compare_by="agent",
                compare_a="claude-code",
                compare_b="codex",
                compare_metric="reward",
            )
            assert await _names(session, **cc_beats_codex, compare_agg="best") == {
                "cmp-reward",
                "cmp-margin",
                "cmp-avg",
                "cmp-median",
            }
            assert await _names(session, **cc_beats_codex, compare_agg="avg") == {
                "cmp-reward",
                "cmp-margin",
            }
            assert await _names(session, **cc_beats_codex, compare_agg="median") == {
                "cmp-reward",
                "cmp-margin",
                "cmp-median",
            }

            # --- Compare A vs B inside an OR-group ---
            assert await _names(session, or_groups=[{"compare": cc_beats_codex}]) == {
                "cmp-reward",
                "cmp-margin",
                "cmp-avg",
                "cmp-median",
            }
            assert await _names(
                session,
                or_groups=[{"compare": {**cc_beats_codex, "compare_agg": "median"}}],
            ) == {"cmp-reward", "cmp-margin", "cmp-median"}

            # --- Pass rate range filter (percent 0-100) ---
            # alpha = 1 pass / 1 trial = 100%; cmp-avg = 1 pass / 2 = 50%.
            assert await _names(session, pass_rate_min=90) == {"alpha"}
            assert await _names(session, pass_rate_min=45, pass_rate_max=55) == {
                "cmp-avg"
            }
    finally:
        await engine.dispose()


async def test_browse_boolean_no_is_complement():
    """``has_error`` / ``has_trajectory`` "No" is the complement of "Yes": the
    task ran a real trial and NONE is positive. A task with a mix of positive
    and negative trials therefore counts as "Yes" and is excluded from "No"."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        await _insert_mixed_trial_task(engine)
        async with maker() as session:
            # delta HAS an errored trial -> "Yes"; excluded from "No".
            assert await _names(session, has_error=True) == {"beta", "delta"}
            assert await _names(session, has_error=False) == {"alpha"}
            # delta HAS a trajectory trial -> "Yes"; excluded from "No".
            assert await _names(session, has_trajectory=True) == {
                "alpha",
                "delta",
            }
            assert await _names(session, has_trajectory=False) == {"beta"}
    finally:
        await engine.dispose()
