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
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (  # type: ignore[attr-defined]
    async_sessionmaker,
    create_async_engine,
)

import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.core.endpoints import browse_tasks_core, set_task_default_version_core
from oddish.core.task_browse_rollup import refresh_task_browse_rollups_for_tasks
from oddish.db.models import Base
from oddish.schemas import TaskBrowseItem, TaskBrowseTrial


URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]

ORG = "org1"


def _preview_trials(item: TaskBrowseItem) -> list[TaskBrowseTrial]:
    return [trial for group in item.trial_groups for trial in group.latest_trials]


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
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await refresh_task_browse_rollups_for_tasks(
            session, ["t-old", "t-new", "t-pct", "t-und"]
        )
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
        assert _preview_trials(old) == []
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


async def test_browse_returns_one_canonical_default_model_group():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            await session.execute(
                text(
                    """
                    insert into trials
                        (id,name,task_id,task_version_id,experiment_id,org_id,
                         agent,provider,queue_key,model,timeout_minutes,environment,
                         harbor_config,status,origin,is_probe,reward,finished_at,
                         attempts,max_attempts,heartbeat_failure_count,
                         has_trajectory,created_at,updated_at)
                    values
                        ('tr-default','tr-default','t-new','v-new','exp-real','org1',
                         'claude','anthropic','q',' default ',30,'modal','{}'::jsonb,
                         'SUCCESS','oddish',false,0.5,now(),1,6,0,false,now(),now())
                    """
                )
            )
            await refresh_task_browse_rollups_for_tasks(session, ["t-new"])
            await session.commit()
        async with maker() as session:
            response = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)

        item = next(item for item in response.items if item.id == "t-new")
        assert len(item.trial_groups) == 1
        group = item.trial_groups[0]
        assert group.model_key == "default"
        assert group.model_label is None
        assert group.trial_count == 2
        assert group.reward_sum == 1.5
        assert {trial.id for trial in group.latest_trials} == {
            "tr-real",
            "tr-default",
        }
    finally:
        await engine.dispose()


async def test_default_browse_only_queries_page_scoped_trials():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []

    def capture_sql(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    try:
        await _setup(engine)
        event.listen(engine.sync_engine, "before_cursor_execute", capture_sql)
        try:
            async with maker() as session:
                response = await browse_tasks_core(
                    session, org_id=ORG, limit=10, offset=0
                )
            assert response.items
            trial_statements = [
                statement
                for statement in statements
                if "from trials" in statement or "join trials" in statement
            ]
            assert trial_statements
            assert all(
                "(trials.task_id, trials.task_version_id) in" in statement
                for statement in trial_statements
            )
            assert all(
                "group by trials.task_id" not in statement
                for statement in trial_statements
            )
            mutable_reads = [
                statement
                for statement in trial_statements
                if "analysis_costs" not in statement
            ]
            assert len(mutable_reads) == 1
            assert "trials.status in" in mutable_reads[0]
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture_sql)
    finally:
        await engine.dispose()


async def test_default_browse_overlays_live_trial_state_and_cost():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            await session.execute(
                text(
                    """
                    insert into trials
                        (id,name,task_id,task_version_id,experiment_id,org_id,
                         agent,provider,queue_key,timeout_minutes,environment,
                         harbor_config,status,origin,is_probe,cost_usd,attempts,
                         max_attempts,heartbeat_failure_count,has_trajectory,
                         created_at,updated_at)
                    values
                        ('tr-live','tr-live','t-new','v-new','exp-real','org1',
                         'claude','anthropic','q',30,'modal','{}'::jsonb,
                         'RUNNING','oddish',false,0.25,1,6,0,false,now(),now())
                    """
                )
            )
            await refresh_task_browse_rollups_for_tasks(session, ["t-new"])
            await session.commit()
        async with maker() as session:
            await session.execute(
                text("update trials set cost_usd=0.75 where id='tr-live'")
            )
            await session.commit()
        async with maker() as session:
            response = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)
        item = next(item for item in response.items if item.id == "t-new")
        assert item.total_trials == 2
        assert item.completed_trials == 1
        assert item.trial_status_counts["running"] == 1
        assert item.cost_usd == 0.75
        assert {trial.id for trial in _preview_trials(item)} == {
            "tr-real",
            "tr-live",
        }
    finally:
        await engine.dispose()


async def test_default_version_switch_refreshes_selected_card_projection():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        async with maker() as session:
            await session.execute(
                text(
                    """
                    insert into task_versions
                        (id,task_id,version,task_path,created_at,updated_at)
                    values ('v-new-2','t-new',2,'p',now(),now())
                    """
                )
            )
            await session.execute(
                text(
                    """
                    insert into trials
                        (id,name,task_id,task_version_id,experiment_id,org_id,
                         agent,provider,queue_key,timeout_minutes,environment,
                         harbor_config,status,origin,is_probe,reward,attempts,
                         max_attempts,heartbeat_failure_count,has_trajectory,
                         finished_at,created_at,updated_at)
                    values
                        ('tr-v2','tr-v2','t-new','v-new-2','exp-real','org1',
                         'codex','openai','q',30,'modal','{}'::jsonb,
                         'SUCCESS','oddish',false,0.5,1,6,0,false,now(),now(),now())
                    """
                )
            )
            await session.commit()
        async with maker() as session:
            await set_task_default_version_core(
                session,
                task_id="t-new",
                version=2,
                org_id=ORG,
            )
            await session.commit()
        async with maker() as session:
            response = await browse_tasks_core(session, org_id=ORG, limit=10, offset=0)
        item = next(item for item in response.items if item.id == "t-new")
        assert item.current_version == 2
        assert item.current_version_id == "v-new-2"
        assert item.total_trials == 1
        assert [trial.id for trial in _preview_trials(item)] == ["tr-v2"]
    finally:
        await engine.dispose()


async def test_browse_org_scope_and_standalone_global_scope():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _setup(engine)
        statements = """
            insert into organizations
                (id,name,slug,plan,settings,is_active,created_at,updated_at)
            values ('org2','Other','other','free','{}'::jsonb,true,now(),now());
            insert into experiments
                (id,name,org_id,is_public,created_at,updated_at)
            values ('exp-other','Other Exp','org2',false,now(),now());
            insert into tasks
                (id,name,org_id,"user",priority,status,task_path,tags,
                 run_analysis,run_probe,created_at,updated_at)
            values
                ('t-other','foreign-task','org2','u','LOW','COMPLETED','p',
                 '{}'::jsonb,false,false,now(),now());
            insert into task_versions
                (id,task_id,version,task_path,created_at,updated_at)
            values ('v-other','t-other',1,'p',now(),now());
            update tasks set current_version_id='v-other' where id='t-other';
            insert into trials
                (id,name,task_id,task_version_id,experiment_id,org_id,
                 agent,provider,queue_key,timeout_minutes,environment,
                 harbor_config,status,origin,is_probe,reward,attempts,
                 max_attempts,heartbeat_failure_count,has_trajectory,
                 finished_at,created_at,updated_at)
            values
                ('tr-other','tr-other','t-other','v-other','exp-other','org2',
                 'codex','openai','q',30,'modal','{}'::jsonb,'SUCCESS',
                 'oddish',false,1.0,1,6,0,false,now(),now(),now())
        """
        async with maker() as session:
            for statement in statements.split(";"):
                if statement.strip():
                    await session.execute(text(statement))
            await refresh_task_browse_rollups_for_tasks(session, ["t-other"])
            await session.commit()
        async with maker() as session:
            hosted = await browse_tasks_core(session, org_id=ORG, limit=20, offset=0)
            standalone = await browse_tasks_core(
                session, org_id=None, limit=20, offset=0
            )
        assert "foreign-task" not in {item.name for item in hosted.items}
        assert "foreign-task" in {item.name for item in standalone.items}
    finally:
        await engine.dispose()


async def test_concurrent_rollup_refreshes_do_not_lose_terminal_trials():
    engine = create_async_engine(URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    try:
        await _setup(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    insert into trials
                        (id,name,task_id,task_version_id,experiment_id,org_id,
                         agent,provider,queue_key,timeout_minutes,environment,
                         harbor_config,status,origin,is_probe,attempts,max_attempts,
                         heartbeat_failure_count,has_trajectory,created_at,updated_at)
                    values
                        ('tr-a','tr-a','t-new','v-new','exp-real','org1',
                         'claude','anthropic','q',30,'modal','{}'::jsonb,
                         'QUEUED','oddish',false,0,6,0,false,now(),now()),
                        ('tr-b','tr-b','t-new','v-new','exp-real','org1',
                         'claude','anthropic','q',30,'modal','{}'::jsonb,
                         'QUEUED','oddish',false,0,6,0,false,now(),now())
                    """
                )
            )

        async def settle_first():
            async with maker() as session:
                await session.execute(
                    text(
                        "update trials set status='SUCCESS', reward=1, "
                        "finished_at=now() where id='tr-a'"
                    )
                )
                await refresh_task_browse_rollups_for_tasks(session, ["t-new"])
                first_locked.set()
                await release_first.wait()
                await session.commit()

        async def settle_second():
            await first_locked.wait()
            async with maker() as session:
                await session.execute(
                    text(
                        "update trials set status='SUCCESS', reward=1, "
                        "finished_at=now() where id='tr-b'"
                    )
                )
                second_started.set()
                await refresh_task_browse_rollups_for_tasks(session, ["t-new"])
                await session.commit()

        first = asyncio.create_task(settle_first())
        second = asyncio.create_task(settle_second())
        await second_started.wait()
        await asyncio.sleep(0.05)
        release_first.set()
        await asyncio.gather(first, second)

        async with maker() as session:
            summary = await session.scalar(
                text("select browse_rollup from task_versions where id='v-new'")
            )
        assert summary["total_trials"] == 3
        assert summary["completed_trials"] == 3
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
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await refresh_task_browse_rollups_for_tasks(session, ["t-c"])
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
        previews = _preview_trials(item)
        assert [trial.id for trial in previews] == ["tr-src"]
        assert previews[0].agent == "claude"
        assert previews[0].model == "sonnet"
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
