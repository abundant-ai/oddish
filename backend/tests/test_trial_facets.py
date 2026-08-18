"""Correctness tests for the ``trial_facets`` vocabulary behind browse facets.

Mirrors ``test_browse_filters.py``: schema is built with
``Base.metadata.create_all`` on an empty Postgres (``ODDISH_DATABASE_URL``);
skips when unset.

Dataset (after ``_setup``): org1 has one live current-version trial with the
full facet spread (model, stage, classification), a live NULL-model trial, a
probe, an old-version trial, a soft-deleted trial, and a superseded trial;
org2 has one live trial sharing org1's environment. Only the two live org1
trials and the org2 trial may contribute vocabulary.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import browse_task_facets_core
from oddish.core.trial_facets import (
    facet_rows_for_trial,
    record_trial_facets,
    rebuild_trial_facets_core,
)
from oddish.db.models import Base, TrialFacetModel
from oddish.queue import _bulk_insert_trials

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
            values ('org1','O','o','free','{}'::jsonb,true,now(),now()),
                   ('org2','O2','o2','free','{}'::jsonb,true,now(),now());
            insert into experiments (id,name,org_id,is_public,created_at,updated_at)
            values ('exp-1','E1','org1',false,now(),now()),
                   ('exp-2','E2','org2',false,now(),now());
            insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at)
            values ('t-1','one','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now()),
                   ('t-2','two','org2','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now());
            insert into task_versions (id,task_id,version,task_path,created_at,updated_at)
            values ('v-1','t-1',1,'p',now(),now()),
                   ('v-0','t-1',0,'p',now(),now()),
                   ('v-2','t-2',1,'p',now(),now());
            update tasks set current_version_id='v-1' where id='t-1';
            update tasks set current_version_id='v-2' where id='t-2';
            insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,analysis,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at)
            values
              ('tr-live','tr-live','t-1','v-1','exp-1','org1','claude-code','anthropic','q',30,'modal','{}'::jsonb,'{"classification":"env_issue"}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-nullm','tr-nullm','t-1','v-1','exp-1','org1','nop','oddish','q',30,'modal','{}'::jsonb,null,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-probe','tr-probe','t-1','v-1','exp-1','org1','gemini-cli','google','q',30,'modal','{}'::jsonb,null,'SUCCESS','oddish',true,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-old','tr-old','t-1','v-0','exp-1','org1','old-agent','anthropic','q',30,'modal','{}'::jsonb,null,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-del','tr-del','t-1','v-1','exp-1','org1','deleted-agent','anthropic','q',30,'modal','{}'::jsonb,null,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-sup','tr-sup','t-1','v-1','exp-1','org1','ghost-agent','anthropic','q',30,'modal','{}'::jsonb,null,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now()),
              ('tr-org2','tr-org2','t-2','v-2','exp-2','org2','org2-agent','openai','q',30,'modal','{}'::jsonb,'{"classification":"infra"}'::jsonb,'SUCCESS','oddish',false,1.0,null,100,50,0,10,true,now(),1,6,0,now(),now());
            update trials set model='claude-fable-5', harbor_stage='completed' where id='tr-live';
            update trials set model='m2' where id='tr-org2';
            update trials set deleted_at=now() where id='tr-del';
            update trials set superseded_by_trial_id='tr-live' where id='tr-sup';
        """
        for stmt in stmts.split(";"):
            if stmt.strip():
                await c.execute(text(stmt))


async def test_rebuild_and_facets_read():
    """One rebuild derives exactly the browse-visible vocabulary, per org."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            orgs, _rows = await rebuild_trial_facets_core(session)
            await session.commit()
            assert orgs == 2

            facets = await browse_task_facets_core(session, org_id=ORG)
            # Probe / superseded / old-version / soft-deleted trials must not
            # contribute; the two live trials define org1's vocabulary.
            assert facets.agents == ["claude-code", "nop"]
            assert facets.models == ["claude-fable-5"]
            assert {(p.agent, p.model) for p in facets.agent_models} == {
                ("claude-code", "claude-fable-5"),
                ("nop", None),
            }
            assert facets.providers == ["anthropic", "oddish"]
            assert facets.environments == ["modal"]
            assert facets.harbor_stages == ["completed"]
            assert facets.analysis_classifications == ["env_issue"]
            assert facets.experiments == []

            # The critical isolation property, both directions.
            other = await browse_task_facets_core(session, org_id="org2")
            assert other.agents == ["org2-agent"]
            assert other.analysis_classifications == ["infra"]
            assert "claude-code" not in other.agents

            # Unscoped read (no hosted caller does this) is the cross-org
            # union; 'modal' exists in both orgs and must appear once.
            union = await browse_task_facets_core(session, org_id=None)
            assert union.agents == ["claude-code", "nop", "org2-agent"]
            assert union.environments == ["modal"]
    finally:
        await engine.dispose()


async def test_rebuild_prunes_stale_values():
    """Write-through never removes; the rebuild is the exactness authority.

    ``prune_grace_seconds=0``: the grace exists to spare in-flight
    write-throughs in production; here rows written seconds ago must prune.
    """
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            await rebuild_trial_facets_core(session)
            await session.commit()

            # Supersede the only trial carrying model/stage/classification:
            # the next rebuild must prune exactly those values.
            await session.execute(
                text(
                    "update trials set superseded_by_trial_id='tr-nullm' "
                    "where id='tr-live'"
                )
            )
            await rebuild_trial_facets_core(session, prune_grace_seconds=0)
            await session.commit()
            after = await browse_task_facets_core(session, org_id=ORG)
            assert after.agents == ["nop"]
            assert after.models == []
            assert after.harbor_stages == []
            assert after.analysis_classifications == []
    finally:
        await engine.dispose()


async def test_write_through_is_instant_and_idempotent():
    """record_trial_facets makes values filterable with no rebuild involved."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            # Nothing rebuilt yet: the vocabulary starts empty.
            assert (await browse_task_facets_core(session, org_id=ORG)).agents == []

            rows = facet_rows_for_trial(
                org_id=ORG,
                agent="fresh-agent",
                model="fresh-model",
                provider="anthropic",
                environment="modal",
            )
            await record_trial_facets(session, rows)
            await record_trial_facets(session, rows)  # idempotent re-record
            await session.commit()

            facets = await browse_task_facets_core(session, org_id=ORG)
            assert facets.agents == ["fresh-agent"]
            assert facets.models == ["fresh-model"]
            assert {(p.agent, p.model) for p in facets.agent_models} == {
                ("fresh-agent", "fresh-model")
            }

        # Probes and org-less (standalone) trials contribute nothing.
        assert facet_rows_for_trial(org_id=ORG, agent="x", is_probe=True) == set()
        assert facet_rows_for_trial(org_id=None, agent="x") == set()
    finally:
        await engine.dispose()


async def test_rebuild_survives_concurrent_write_through():
    """A write-through committing mid-rebuild must never be lost.

    Fires a full trial+vocabulary commit right after the rebuild's FIRST
    statement (the t0 clock read): mark-and-prune must keep it — the scan
    re-derives the value, the fresh ``written_at`` clears the prune, and
    the upsert tolerates the existing row. A wholesale delete+reinsert
    (either ordering) loses one of those paths.
    """
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as sweeper:
            real_execute = sweeper.execute
            fired = {"done": False}

            async def execute_with_race(*args, **kwargs):
                result = await real_execute(*args, **kwargs)
                if not fired["done"]:
                    fired["done"] = True
                    async with maker() as writer:
                        await writer.execute(
                            text(
                                "insert into trials (id,name,task_id,task_version_id,experiment_id,org_id,agent,provider,queue_key,timeout_minutes,environment,harbor_config,analysis,status,origin,is_probe,reward,error_message,input_tokens,output_tokens,cache_tokens,total_steps,has_trajectory,finished_at,attempts,max_attempts,heartbeat_failure_count,created_at,updated_at) "
                                "values ('tr-race','tr-race','t-1','v-1','exp-1','org1','race-agent','anthropic','q',30,'modal','{}'::jsonb,null,'QUEUED','oddish',false,null,null,100,50,0,10,false,null,0,6,0,now(),now())"
                            )
                        )
                        await record_trial_facets(
                            writer,
                            facet_rows_for_trial(org_id=ORG, agent="race-agent"),
                        )
                        await writer.commit()
                return result

            sweeper.execute = execute_with_race
            await rebuild_trial_facets_core(sweeper)
            await sweeper.commit()

        async with maker() as session:
            facets = await browse_task_facets_core(session, org_id=ORG)
            assert "race-agent" in facets.agents  # re-derived by the scan
            assert "claude-code" in facets.agents  # rebuilt rows intact
    finally:
        await engine.dispose()


async def test_write_through_tolerates_missing_table():
    """Deploy-before-migrate: the auxiliary vocabulary write must never abort
    the primary transaction (the quotas/costs missing-table precedent)."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with engine.begin() as c:
            await c.execute(text("drop table trial_facets"))
        async with maker() as session:
            await session.execute(
                text(
                    'insert into tasks (id,name,org_id,"user",priority,status,task_path,link,tags,run_analysis,run_probe,created_at,updated_at) '
                    "values ('t-mt','mt','org1','u','LOW','COMPLETED','p',null,'{}'::jsonb,false,false,now(),now())"
                )
            )
            await record_trial_facets(
                session, facet_rows_for_trial(org_id=ORG, agent="window-agent")
            )
            await session.commit()
            count = (
                await session.execute(
                    text("select count(*) from tasks where id='t-mt'")
                )
            ).scalar()
            assert count == 1
        async with engine.begin() as c:
            await c.run_sync(TrialFacetModel.__table__.create)
    finally:
        await engine.dispose()


async def test_bulk_insert_funnel_writes_vocabulary():
    """The queue's bulk-insert chokepoint records vocabulary in-transaction."""
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            base = dict(
                task_id="t-1",
                task_version_id="v-1",
                experiment_id="exp-1",
                org_id=ORG,
                provider="daytona",
                queue_key="q",
                model="bulk-model",
                timeout_minutes=30,
                environment="daytona",
                harbor_config=None,
                harbor_sha=None,
                max_attempts=6,
            )
            await _bulk_insert_trials(
                session,
                [
                    {
                        **base,
                        "id": "tr-bulk-1",
                        "name": "tr-bulk-1",
                        "agent": "bulk-agent",
                        "is_probe": False,
                    },
                    {
                        **base,
                        "id": "tr-bulk-2",
                        "name": "tr-bulk-2",
                        "agent": "probe-agent",
                        "is_probe": True,
                    },
                ],
            )
            await session.commit()

            # No rebuild ran: everything visible came from the funnel, and the
            # probe row contributed nothing.
            facets = await browse_task_facets_core(session, org_id=ORG)
            assert facets.agents == ["bulk-agent"]
            assert facets.models == ["bulk-model"]
            assert facets.providers == ["daytona"]
            assert facets.environments == ["daytona"]
    finally:
        await engine.dispose()
