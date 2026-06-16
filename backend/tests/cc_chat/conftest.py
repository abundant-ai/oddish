# backend/tests/cc_chat/conftest.py
import os
import pytest
import pytest_asyncio
import models  # noqa: F401  registers cloud tables on the shared Base
from oddish.db.models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

URL = os.environ.get("ODDISH_DATABASE_URL")

ORG = "org_cc_test"


async def reset_and_seed(engine):
    async with engine.begin() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
        await c.run_sync(Base.metadata.create_all)
        await c.execute(
            text(
                "insert into organizations (id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "values (:id, 'CC Test', 'cc-test', 'free', '{}', true, now(), now())"
            ),
            {"id": ORG},
        )


async def seed_session(maker, *, session_id="cs_1", status="active", scope_kind="experiment", scope_id="exp_1"):
    from models import ChatSession
    async with maker() as s:
        s.add(ChatSession(
            id=session_id, org_id=ORG, user_id="u1",
            scope_kind=scope_kind, scope_id=scope_id,
            status=status, daytona_session_id="cc",
        ))
        await s.commit()


@pytest_asyncio.fixture
async def db():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    engine = create_async_engine(URL)
    await reset_and_seed(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def seed_task_with_trials(maker, *, task_id="task_1", versions=(1, 2), trials_per_version=1):
    """Seed an experiment, a task, its task_versions, current_version pointer,
    and trials via raw INSERTs (avoids ORM enum/NOT-NULL pitfalls). Returns
    {version_int: [trial_id, ...]}. Trials get trial_s3_key=None so
    resolve_trial_s3_prefix falls back to tasks/{task_id}/trials/{trial_id}/."""
    from sqlalchemy import text
    out: dict[int, list[str]] = {}
    exp_id = f"exp_{task_id}"
    async with maker() as s:
        await s.execute(
            text(
                "insert into experiments (id,name,org_id,is_public,created_at,updated_at) "
                "values (:id,'demo-exp',:org,false,now(),now())"
            ),
            {"id": exp_id, "org": ORG},
        )
        await s.execute(
            text(
                'insert into tasks (id,name,org_id,"user",priority,status,task_path,'
                "tags,run_analysis,run_probe,created_at,updated_at) "
                "values (:id,'demo-task',:org,'u','LOW','COMPLETED','p','{}'::jsonb,"
                "false,false,now(),now())"
            ),
            {"id": task_id, "org": ORG},
        )
        max_v = max(versions)
        for v in versions:
            vid = f"{task_id}-v{v}"
            await s.execute(
                text(
                    "insert into task_versions (id,task_id,version,task_path,created_at,updated_at) "
                    "values (:id,:tid,:v,'p',now(),now())"
                ),
                {"id": vid, "tid": task_id, "v": v},
            )
            ids = []
            for i in range(trials_per_version):
                trial_id = f"{task_id}-{v}{i}"
                await s.execute(
                    text(
                        "insert into trials (id,name,task_id,task_version_id,experiment_id,"
                        "org_id,agent,provider,queue_key,status,origin,is_probe,attempts,"
                        "max_attempts,created_at,updated_at) "
                        "values (:id,:id,:tid,:vid,:eid,:org,'claude','anthropic','q',"
                        "'SUCCESS','oddish',false,1,6,now(),now())"
                    ),
                    {"id": trial_id, "tid": task_id, "vid": vid, "eid": exp_id, "org": ORG},
                )
                ids.append(trial_id)
            out[v] = ids
        await s.execute(
            text("update tasks set current_version_id=:vid where id=:tid"),
            {"vid": f"{task_id}-v{max_v}", "tid": task_id},
        )
        await s.commit()
    return out
