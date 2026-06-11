"""Local unit test for the preview seed: building the full schema then
seeding yields a consistent, deterministic, convergent preview DB.

The schema is built with ``Base.metadata.create_all`` -- oddish and backend
models register on the same ``DeclarativeBase`` -- rather than replaying the
Alembic chain. The real pipeline only ever applies Alembic incrementally onto
a branch whose schema Supabase already cloned, never from an empty database,
so a from-scratch chain replay is not a supported path.

Run against an empty Postgres by setting ``ODDISH_DATABASE_URL`` (the test
builds the schema itself); it skips otherwise. The deploy-path gate is the
real seed step in the prepare-preview-database job, which seeds the actual
branch -- if the seed breaks against the real schema, that job fails."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  registers the cloud tables on the shared Base
import preview_seed
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]


@pytest.fixture(autouse=True)
def _clerk_org(monkeypatch):
    monkeypatch.setenv("ODDISH_PREVIEW_CLERK_ORG_ID", "org_seedtest")


async def _count(engine, sql):
    async with engine.connect() as c:
        return (await c.execute(text(sql))).scalar_one()


async def test_seed_populates_and_is_idempotent_and_convergent():
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
            await conn.run_sync(Base.metadata.create_all)

        await preview_seed.seed(engine)
        assert await _count(engine, "select count(*) from organizations where id like 'seed-%'") == 1
        assert await _count(engine, "select count(*) from organizations where clerk_org_id = 'org_seedtest'") == 1
        assert await _count(engine, "select count(*) from tasks where id='seed-task-a' and current_version_id='seed-task-a-v2'") == 1
        assert await _count(engine, "select count(*) from task_experiments where task_id like 'seed-%' and deleted_at is not null") >= 1

        tasks_before = await _count(engine, "select count(*) from tasks where id like 'seed-%'")
        await preview_seed.seed(engine)  # idempotent
        assert await _count(engine, "select count(*) from tasks where id like 'seed-%'") == tasks_before

        orig = preview_seed._fixtures  # convergent
        def _edited():
            f = orig()
            f["organizations"][0]["name"] = "Edited Org"
            return f
        preview_seed._fixtures = _edited  # type: ignore[assignment]
        try:
            await preview_seed.seed(engine)
        finally:
            preview_seed._fixtures = orig  # type: ignore[assignment]
        async with engine.connect() as c:
            name = (await c.execute(text("select name from organizations where id='seed-org'"))).scalar_one()
        assert name == "Edited Org"
    finally:
        await engine.dispose()


async def test_seed_reclaims_clerk_org_id_from_a_preexisting_row():
    """A reused or once---with-data branch may already hold a row with the
    seeded clerk_org_id on a different id (e.g. cloned prod data). The seed
    must free that UNIQUE(clerk_org_id) mapping rather than fail with a
    duplicate-key error."""
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text(
                "insert into organizations "
                "(id, name, slug, clerk_org_id, plan, settings, is_active, "
                "created_at, updated_at) values "
                "('preexisting-org', 'Real Org', 'real-org', 'org_seedtest', "
                "'free', '{}'::jsonb, true, "
                "'2025-01-01T00:00:00+00', '2025-01-01T00:00:00+00')"
            ))

        await preview_seed.seed(engine)

        # the seeded org claimed the clerk_org_id ...
        assert await _count(
            engine,
            "select count(*) from organizations where id='seed-org' "
            "and clerk_org_id='org_seedtest'",
        ) == 1
        # ... and the pre-existing row was freed (mapping nulled, no duplicate)
        assert await _count(
            engine,
            "select count(*) from organizations where id='preexisting-org' "
            "and clerk_org_id is null",
        ) == 1
    finally:
        await engine.dispose()


# --- prod-sample tests -------------------------------------------------------
# A second database on the same Postgres server stands in for production.

SAMPLE_KEY = "77"


def _src_url() -> str:
    base, _, _db = URL.rpartition("/")
    return f"{base}/seed_sample_src"


async def _make_source_db():
    admin = create_async_engine(URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(text("drop database if exists seed_sample_src with (force)"))
            await c.execute(text("create database seed_sample_src"))
    finally:
        await admin.dispose()

    src = create_async_engine(_src_url())
    t = Base.metadata.tables
    async with src.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.execute(t["organizations"].insert(), [
            {"id": "org-a", "name": "Real A", "slug": "real-a",
             "clerk_org_id": "org_real_a"},
            {"id": "org-b", "name": "Real B", "slug": "real-b",
             "clerk_org_id": "org_real_b"},
        ])
        await c.execute(t["users"].insert(), [
            {"id": "u-a1", "org_id": "org-a", "email": "real-a1@corp.com",
             "name": "Alice Real", "role": "owner", "github_username": "alice"},
            {"id": "u-b1", "org_id": "org-b", "email": "real-b1@corp.com",
             "name": "Bob Real", "role": "member", "github_username": None},
        ])
        await c.execute(t["experiments"].insert(), [
            {"id": "exp-a", "name": "Exp A", "org_id": "org-a",
             "is_public": True, "public_token": "tok-a", "deleted_at": None},
            {"id": "exp-b", "name": "Exp B", "org_id": "org-b",
             "is_public": False, "public_token": None, "deleted_at": None},
            {"id": "exp-del", "name": "Deleted", "org_id": "org-a",
             "is_public": False, "public_token": None,
             "deleted_at": preview_seed.SEED_EPOCH},
        ])
        await c.execute(t["tasks"].insert(), [
            {"id": "task-solo", "name": "solo-task", "org_id": "org-a",
             "created_by_user_id": "u-a1", "user": "real-a1@corp.com",
             "status": "COMPLETED", "task_path": "p/solo", "tags": {}},
            {"id": "task-dup-a", "name": "dup-task", "org_id": "org-a",
             "created_by_user_id": "u-a1", "user": "real-a1@corp.com",
             "status": "COMPLETED", "task_path": "p/dup-a", "tags": {}},
            {"id": "task-dup-b", "name": "dup-task", "org_id": "org-b",
             "created_by_user_id": "u-b1", "user": "real-b1@corp.com",
             "status": "FAILED", "task_path": "p/dup-b", "tags": {}},
            {"id": "task-run", "name": "running-task", "org_id": "org-a",
             "created_by_user_id": "u-a1", "user": "real-a1@corp.com",
             "status": "RUNNING", "task_path": "p/run", "tags": {}},
        ])
        await c.execute(t["task_versions"].insert(), [
            {"id": "ver-solo-1", "task_id": "task-solo", "version": 1,
             "task_path": "p/solo"},
            {"id": "ver-solo-2", "task_id": "task-solo", "version": 2,
             "task_path": "p/solo"},
            {"id": "ver-dup-a", "task_id": "task-dup-a", "version": 1,
             "task_path": "p/dup-a"},
            {"id": "ver-dup-b", "task_id": "task-dup-b", "version": 1,
             "task_path": "p/dup-b"},
        ])
        await c.execute(
            t["tasks"].update().where(t["tasks"].c.id == "task-solo")
            .values(current_version_id="ver-solo-2")
        )
        await c.execute(t["task_experiments"].insert(), [
            {"task_id": "task-solo", "experiment_id": "exp-a"},
            {"task_id": "task-dup-a", "experiment_id": "exp-a"},
            {"task_id": "task-dup-b", "experiment_id": "exp-b"},
            {"task_id": "task-run", "experiment_id": "exp-a"},
        ])
        await c.execute(t["trials"].insert(), [
            {"id": "tr-ok", "name": "tr-ok", "task_id": "task-solo",
             "task_version_id": "ver-solo-2", "experiment_id": "exp-a",
             "org_id": "org-a", "agent": "claude", "provider": "anthropic",
             "queue_key": "q", "timeout_minutes": 30, "environment": "modal",
             "harbor_config": {}, "status": "SUCCESS", "origin": "oddish",
             "result": {"reward": 1}, "superseded_by_trial_id": None},
            {"id": "tr-running", "name": "tr-running", "task_id": "task-solo",
             "task_version_id": "ver-solo-2", "experiment_id": "exp-a",
             "org_id": "org-a", "agent": "claude", "provider": "anthropic",
             "queue_key": "q", "timeout_minutes": 30, "environment": "modal",
             "harbor_config": {}, "status": "RUNNING", "origin": "oddish",
             "result": None, "superseded_by_trial_id": None},
            {"id": "tr-superseded", "name": "tr-superseded",
             "task_id": "task-solo", "task_version_id": "ver-solo-2",
             "experiment_id": "exp-a", "org_id": "org-a", "agent": "claude",
             "provider": "anthropic", "queue_key": "q", "timeout_minutes": 30,
             "environment": "modal", "harbor_config": {}, "status": "FAILED",
             "origin": "oddish", "result": None,
             "superseded_by_trial_id": "tr-running"},
        ])
        await c.execute(t["worker_jobs"].insert(), [
            {"id": "wj-done", "kind": "TRIAL", "status": "SUCCESS",
             "queue_key": "q", "subject_table": "trials",
             "subject_id": "tr-ok", "org_id": "org-a",
             "parent_job_id": None},
            {"id": "wj-live", "kind": "TRIAL", "status": "RUNNING",
             "queue_key": "q", "subject_table": "trials",
             "subject_id": "tr-ok", "org_id": "org-a",
             "parent_job_id": None},
            {"id": "wj-child", "kind": "VERDICT", "status": "FAILED",
             "queue_key": "q", "subject_table": "tasks",
             "subject_id": "task-solo", "org_id": "org-a",
             "parent_job_id": "wj-live"},
        ])
        await c.execute(t["skills"].insert(), [
            {"id": "sk-a", "org_id": "org-a", "created_by_user_id": "u-a1",
             "name": "dup-skill", "description": "a"},
            {"id": "sk-b", "org_id": "org-b", "created_by_user_id": "u-b1",
             "name": "dup-skill", "description": "b"},
        ])
        await c.execute(t["skill_files"].insert(), [
            {"id": "skf-a1", "skill_id": "sk-a", "relative_path": "SKILL.md",
             "content": "# a"},
            {"id": "skf-b1", "skill_id": "sk-b", "relative_path": "SKILL.md",
             "content": "# b"},
        ])
        await c.execute(t["documents"].insert(), [
            {"id": "doc-1", "org_id": "org-a", "created_by_user_id": "u-a1",
             "title": "Doc One", "source_type": "text"},
        ])
    return src


async def test_sample_prod_subset_is_deterministic_remapped_and_scrubbed():
    src = await _make_source_db()
    try:
        s1 = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        s2 = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        assert s1 == s2  # deterministic for a given key

        rows = s1["rows"]
        exp_ids = {e["id"] for e in rows["experiments"]}
        assert exp_ids == {"exp-a", "exp-b"}  # deleted experiment excluded
        for e in rows["experiments"]:
            assert e["org_id"] == "seed-org"
            assert e["is_public"] is False and e["public_token"] is None

        task_ids = {t["id"] for t in rows["tasks"]}
        run = next(t for t in rows["tasks"] if t["id"] == "task-run")
        assert run["status"] == "FAILED"  # in-flight normalized on import
        dup = [t for t in rows["tasks"] if t["name"] == "dup-task"]
        assert len(dup) == 1  # name-deduped for the unique (org, name) index
        for t in rows["tasks"]:
            assert t["org_id"] == "seed-org"
            assert t["user"] == "owner@preview.local"
            assert t["current_version_id"] is None  # deferred to linkage pass
        assert ("tasks", "task-solo", "current_version_id", "ver-solo-2") in s1["linkage"]

        link_tasks = {l["task_id"] for l in rows["task_experiments"]}
        assert link_tasks <= task_ids  # links only for kept tasks

        trial_ids = {t["id"] for t in rows["trials"]}
        running = next(t for t in rows["trials"] if t["id"] == "tr-running")
        assert running["status"] == "FAILED"  # in-flight normalized
        assert running["current_worker_id"] is None
        sup = next(t for t in rows["trials"] if t["id"] == "tr-superseded")
        assert sup["superseded_by_trial_id"] is None  # deferred to linkage pass
        assert ("trials", "tr-superseded", "superseded_by_trial_id",
                "tr-running") in s1["linkage"]

        jobs = {j["id"]: j for j in rows["worker_jobs"]}
        assert "wj-live" not in jobs  # only terminal jobs imported
        assert jobs["wj-child"]["parent_job_id"] is None  # parent not sampled
        assert all(j["org_id"] == "seed-org" for j in jobs.values())

        skills = rows["skills"]
        assert len(skills) == 1  # name-deduped like tasks
        kept_skill = skills[0]["id"]
        assert all(f["skill_id"] == kept_skill for f in rows["skill_files"])
        assert rows["documents"][0]["org_id"] == "seed-org"

        for u in rows["users"]:
            assert u["org_id"] == "seed-org"
            assert u["email"].endswith("@preview.local")
            assert u["clerk_user_id"] is None and u["github_username"] is None
    finally:
        await src.dispose()


async def test_seed_with_sample_loads_links_and_reconciles_drift():
    src = await _make_source_db()
    engine = create_async_engine(URL)
    try:
        sampled = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
            await conn.run_sync(Base.metadata.create_all)
        await preview_seed.seed(engine, sampled=sampled)

        # a JIT reviewer user created AFTER seeding must survive later runs
        async with engine.begin() as c:
            await c.execute(text(
                "insert into users (id, org_id, email, role, name, is_active,"
                " created_at, updated_at) values ('jit-rev', 'seed-org',"
                " 'reviewer@corp.com', 'owner', 'Reviewer', true, now(), now())"
            ))

        assert await _count(
            engine,
            "select count(*) from experiments where org_id='seed-org'"
            " and id not like 'seed-%'") == 2
        assert await _count(
            engine,
            "select count(*) from tasks where id='task-solo'"
            " and current_version_id='ver-solo-2'") == 1  # linkage applied
        assert await _count(
            engine,
            "select count(*) from trials where id='tr-ok'"
            " and (result->>'reward')::int = 1") == 1  # JSONB not double-encoded
        assert await _count(
            engine,
            "select count(*) from trials where id='tr-superseded'"
            " and superseded_by_trial_id='tr-running'") == 1  # linkage applied

        # prod drift: exp-b vanishes from the next draw -> its rows reconcile
        drifted = {
            "rows": {
                name: [r for r in rows if r.get("experiment_id") != "exp-b"
                       and r.get("id") not in ("exp-b", "task-dup-b", "ver-dup-b")
                       and r.get("task_id") != "task-dup-b"]
                for name, rows in sampled["rows"].items()
            },
            "linkage": sampled["linkage"],
        }
        await preview_seed.seed(engine, sampled=drifted)
        assert await _count(
            engine, "select count(*) from experiments where id='exp-b'") == 0
        assert await _count(
            engine, "select count(*) from experiments where id='exp-a'") == 1
        assert await _count(
            engine, "select count(*) from users where id='jit-rev'") == 1
        assert await _count(
            engine, "select count(*) from tasks where id like 'seed-%'") == 2

        # outage tolerance: sampled=None must leave prior samples untouched
        await preview_seed.seed(engine, sampled=None)
        assert await _count(
            engine,
            "select count(*) from experiments where org_id='seed-org'"
            " and id not like 'seed-%'") >= 1
    finally:
        await engine.dispose()
        await src.dispose()
