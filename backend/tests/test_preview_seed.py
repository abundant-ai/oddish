"""Local tests for the preview seed: a faithful prod subset, deterministic,
idempotent, convergent, and safe around reviewer-created data.

The schema is built with ``Base.metadata.create_all`` -- oddish and backend
models register on the same ``DeclarativeBase`` -- rather than replaying the
Alembic chain (the real pipeline only ever applies Alembic incrementally onto
a branch whose schema Supabase already cloned, never from an empty database).
A second database on the same server stands in for production.

Run by setting ``ODDISH_DATABASE_URL`` to an empty Postgres; skips otherwise.
The deploy-path gate is the real seed step in the prepare-preview-database
job, which samples actual prod and seeds the actual branch."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  registers the cloud tables on the shared Base
import preview_seed
from oddish.db.models import Base

URL = os.environ.get("ODDISH_DATABASE_URL")
SAMPLE_KEY = "77"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set"),
]


async def _count(engine, sql):
    async with engine.connect() as c:
        return (await c.execute(text(sql))).scalar_one()


async def _reset_target(engine):
    async with engine.begin() as conn:
        await conn.execute(text("drop schema public cascade"))
        await conn.execute(text("create schema public"))
        await conn.run_sync(Base.metadata.create_all)


def _src_url() -> str:
    base, _, _db = URL.rpartition("/")
    return f"{base}/seed_sample_src"


async def _make_source_db():
    admin = create_async_engine(URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as c:
            await c.execute(
                text("drop database if exists seed_sample_src with (force)")
            )
            await c.execute(text("create database seed_sample_src"))
    finally:
        await admin.dispose()

    src = create_async_engine(_src_url())
    t = Base.metadata.tables
    async with src.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.execute(
            t["organizations"].insert(),
            [
                {
                    "id": "org-a",
                    "name": "Real A",
                    "slug": "real-a",
                    "clerk_org_id": "org_real_a",
                },
                {
                    "id": "org-b",
                    "name": "Real B",
                    "slug": "real-b",
                    "clerk_org_id": "org_real_b",
                },
            ],
        )
        await c.execute(
            t["users"].insert(),
            [
                {
                    "id": "u-a1",
                    "org_id": "org-a",
                    "email": "real-a1@corp.com",
                    "name": "Alice Real",
                    "role": "admin",
                    "github_username": "alice",
                    "clerk_user_id": "user_a1",
                },
                {
                    "id": "u-a2",
                    "org_id": "org-a",
                    "email": "real-a2@corp.com",
                    "name": "Anna Real",
                    "role": "member",
                    "github_username": None,
                    "clerk_user_id": "user_a2",
                },
                {
                    "id": "u-b1",
                    "org_id": "org-b",
                    "email": "real-b1@corp.com",
                    "name": "Bob Real",
                    "role": "member",
                    "github_username": None,
                    "clerk_user_id": "user_b1",
                },
            ],
        )
        await c.execute(
            t["experiments"].insert(),
            [
                {
                    "id": "exp-a",
                    "name": "Exp A",
                    "org_id": "org-a",
                    "owner_user_id": "u-a1",
                    "is_public": True,
                    "public_token": "tok-a",
                    "deleted_at": None,
                },
                {
                    "id": "exp-b",
                    "name": "Exp B",
                    "org_id": "org-b",
                    "owner_user_id": "u-b1",
                    "is_public": False,
                    "public_token": None,
                    "deleted_at": None,
                },
                {
                    "id": "exp-mine",
                    "name": "Mine Exp",
                    "org_id": "org-a",
                    "owner_user_id": "u-a2",
                    "is_public": False,
                    "public_token": None,
                    "deleted_at": None,
                },
                {
                    "id": "exp-del",
                    "name": "Deleted",
                    "org_id": "org-a",
                    "owner_user_id": "u-a1",
                    "is_public": False,
                    "public_token": None,
                    "deleted_at": preview_seed.SEED_EPOCH,
                },
            ],
        )
        await c.execute(
            t["tasks"].insert(),
            [
                {
                    "id": "task-solo",
                    "name": "solo-task",
                    "org_id": "org-a",
                    "created_by_user_id": "u-a1",
                    "user": "real-a1@corp.com",
                    "status": "COMPLETED",
                    "task_path": "p/solo",
                    "tags": {},
                },
                {
                    "id": "task-dup-a",
                    "name": "dup-task",
                    "org_id": "org-a",
                    "created_by_user_id": "u-a1",
                    "user": "real-a1@corp.com",
                    "status": "COMPLETED",
                    "task_path": "p/dup-a",
                    "tags": {},
                },
                {
                    "id": "task-dup-b",
                    "name": "dup-task",
                    "org_id": "org-b",
                    "created_by_user_id": "u-b1",
                    "user": "real-b1@corp.com",
                    "status": "FAILED",
                    "task_path": "p/dup-b",
                    "tags": {},
                },
                {
                    "id": "task-run",
                    "name": "running-task",
                    "org_id": "org-a",
                    "created_by_user_id": "u-a1",
                    "user": "real-a1@corp.com",
                    "status": "RUNNING",
                    "task_path": "p/run",
                    "tags": {},
                },
            ],
        )
        await c.execute(
            t["task_versions"].insert(),
            [
                {
                    "id": "ver-solo-1",
                    "task_id": "task-solo",
                    "version": 1,
                    "task_path": "p/solo",
                },
                {
                    "id": "ver-solo-2",
                    "task_id": "task-solo",
                    "version": 2,
                    "task_path": "p/solo",
                },
                {
                    "id": "ver-dup-a",
                    "task_id": "task-dup-a",
                    "version": 1,
                    "task_path": "p/dup-a",
                },
                {
                    "id": "ver-dup-b",
                    "task_id": "task-dup-b",
                    "version": 1,
                    "task_path": "p/dup-b",
                },
            ],
        )
        await c.execute(
            t["tasks"]
            .update()
            .where(t["tasks"].c.id == "task-solo")
            .values(current_version_id="ver-solo-2")
        )
        await c.execute(
            t["task_experiments"].insert(),
            [
                {"task_id": "task-solo", "experiment_id": "exp-a"},
                {"task_id": "task-dup-a", "experiment_id": "exp-a"},
                {"task_id": "task-dup-b", "experiment_id": "exp-b"},
                {"task_id": "task-run", "experiment_id": "exp-a"},
            ],
        )
        await c.execute(
            t["trials"].insert(),
            [
                {
                    "id": "tr-ok",
                    "name": "tr-ok",
                    "task_id": "task-solo",
                    "task_version_id": "ver-solo-2",
                    "experiment_id": "exp-a",
                    "org_id": "org-a",
                    "agent": "claude",
                    "provider": "anthropic",
                    "queue_key": "q",
                    "timeout_minutes": 30,
                    "environment": "modal",
                    "harbor_config": {},
                    "status": "SUCCESS",
                    "origin": "oddish",
                    "result": {"reward": 1},
                    "superseded_by_trial_id": None,
                },
                {
                    "id": "tr-running",
                    "name": "tr-running",
                    "task_id": "task-solo",
                    "task_version_id": "ver-solo-2",
                    "experiment_id": "exp-a",
                    "org_id": "org-a",
                    "agent": "claude",
                    "provider": "anthropic",
                    "queue_key": "q",
                    "timeout_minutes": 30,
                    "environment": "modal",
                    "harbor_config": {},
                    "status": "RUNNING",
                    "origin": "oddish",
                    "result": None,
                    "superseded_by_trial_id": None,
                },
                {
                    "id": "tr-superseded",
                    "name": "tr-superseded",
                    "task_id": "task-solo",
                    "task_version_id": "ver-solo-2",
                    "experiment_id": "exp-a",
                    "org_id": "org-a",
                    "agent": "claude",
                    "provider": "anthropic",
                    "queue_key": "q",
                    "timeout_minutes": 30,
                    "environment": "modal",
                    "harbor_config": {},
                    "status": "FAILED",
                    "origin": "oddish",
                    "result": None,
                    "superseded_by_trial_id": "tr-running",
                },
            ],
        )
        await c.execute(
            t["worker_jobs"].insert(),
            [
                {
                    "id": "wj-done",
                    "kind": "TRIAL",
                    "status": "SUCCESS",
                    "queue_key": "q",
                    "subject_table": "trials",
                    "subject_id": "tr-ok",
                    "org_id": "org-a",
                    "parent_job_id": None,
                },
                {
                    "id": "wj-live",
                    "kind": "TRIAL",
                    "status": "RUNNING",
                    "queue_key": "q",
                    "subject_table": "trials",
                    "subject_id": "tr-ok",
                    "org_id": "org-a",
                    "parent_job_id": None,
                },
                {
                    "id": "wj-child",
                    "kind": "VERDICT",
                    "status": "FAILED",
                    "queue_key": "q",
                    "subject_table": "tasks",
                    "subject_id": "task-solo",
                    "org_id": "org-a",
                    "parent_job_id": "wj-live",
                },
            ],
        )
        await c.execute(
            t["skills"].insert(),
            [
                {
                    "id": "sk-a",
                    "org_id": "org-a",
                    "created_by_user_id": "u-a1",
                    "name": "skill-a",
                    "description": "a",
                },
            ],
        )
        await c.execute(
            t["skill_files"].insert(),
            [
                {
                    "id": "skf-a1",
                    "skill_id": "sk-a",
                    "relative_path": "SKILL.md",
                    "content": "# a",
                },
            ],
        )
        await c.execute(
            t["documents"].insert(),
            [
                {
                    "id": "doc-1",
                    "org_id": "org-a",
                    "created_by_user_id": "u-a1",
                    "title": "Doc One",
                    "source_type": "text",
                },
            ],
        )
        await c.execute(
            t["tags"].insert(),
            [
                {
                    "id": "t-smoke",
                    "org_id": "org-a",
                    "key": "smoke",
                    "normalized_key": "smoke",
                    "state": "ACTIVE",
                    "owner_user_id": "u-a1",
                    "created_by_user_id": "u-a1",
                },
                {
                    "id": "t-flaky",
                    "org_id": "org-a",
                    "key": "flaky",
                    "normalized_key": "flaky",
                    "state": "ACTIVE",
                    "owner_user_id": "u-a2",
                    "created_by_user_id": "u-a1",
                },
                {
                    "id": "t-bonly",
                    "org_id": "org-b",
                    "key": "bonly",
                    "normalized_key": "bonly",
                    "state": "ACTIVE",
                    "owner_user_id": "u-b1",
                },
                # MERGED tags ARE drawn (the page does its own state filtering);
                # the merged_into_id self-FK points at a sampled tag.
                {
                    "id": "t-merged",
                    "org_id": "org-a",
                    "key": "old",
                    "normalized_key": "old",
                    "state": "MERGED",
                    "merged_into_id": "t-smoke",
                    "owner_user_id": "u-a1",
                },
                # Soft-deleted tags are excluded (deleted_at IS NOT NULL).
                {
                    "id": "t-del",
                    "org_id": "org-a",
                    "key": "gone",
                    "normalized_key": "gone",
                    "state": "ACTIVE",
                    "owner_user_id": "u-a1",
                    "deleted_at": preview_seed.SEED_EPOCH,
                },
            ],
        )
        await c.execute(
            t["tag_assignments"].insert(),
            [
                # DIRECT/ACTIVE onto sampled targets -> kept (one per scope).
                {
                    "id": "ta-task",
                    "tag_id": "t-smoke",
                    "org_id": "org-a",
                    "scope": "TASK",
                    "target_id": "task-solo",
                    "task_id": "task-solo",
                    "source": "DIRECT",
                    "state": "ACTIVE",
                    "assigned_by_user_id": "u-a1",
                },
                {
                    "id": "ta-ver",
                    "tag_id": "t-flaky",
                    "org_id": "org-a",
                    "scope": "VERSION",
                    "target_id": "ver-solo-2",
                    "task_id": "task-solo",
                    "source": "DIRECT",
                    "state": "ACTIVE",
                    "assigned_by_user_id": "u-a2",
                },
                {
                    "id": "ta-exp",
                    "tag_id": "t-smoke",
                    "org_id": "org-a",
                    "scope": "EXPERIMENT",
                    "target_id": "exp-a",
                    "source": "DIRECT",
                    "state": "ACTIVE",
                    "assigned_by_user_id": "u-a1",
                },
                {
                    "id": "ta-bonly",
                    "tag_id": "t-bonly",
                    "org_id": "org-b",
                    "scope": "TASK",
                    "target_id": "task-dup-b",
                    "task_id": "task-dup-b",
                    "source": "DIRECT",
                    "state": "ACTIVE",
                },
                # Experiment-propagated -> excluded (source != DIRECT).
                {
                    "id": "ta-living",
                    "tag_id": "t-smoke",
                    "org_id": "org-a",
                    "scope": "TASK",
                    "target_id": "task-dup-a",
                    "task_id": "task-dup-a",
                    "source": "EXPERIMENT_LIVING",
                    "state": "ACTIVE",
                },
                # Removed -> excluded (state != ACTIVE).
                {
                    "id": "ta-removed",
                    "tag_id": "t-flaky",
                    "org_id": "org-a",
                    "scope": "TASK",
                    "target_id": "task-solo",
                    "task_id": "task-solo",
                    "source": "DIRECT",
                    "state": "REMOVED",
                    "assigned_by_user_id": "u-a1",
                },
                # Target not in the trimmed set (exp-del is deleted) -> excluded.
                {
                    "id": "ta-deltarget",
                    "tag_id": "t-smoke",
                    "org_id": "org-a",
                    "scope": "EXPERIMENT",
                    "target_id": "exp-del",
                    "source": "DIRECT",
                    "state": "ACTIVE",
                },
            ],
        )
    return src


async def test_sample_is_deterministic_and_prod_faithful():
    src = await _make_source_db()
    try:
        s1 = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        s2 = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        assert s1 == s2  # deterministic for a given key

        rows = s1["rows"]
        # identity rows are imported AS-IS: real clerk ids, names, emails
        orgs = {o["id"]: o for o in rows["organizations"]}
        assert orgs["org-a"]["clerk_org_id"] == "org_real_a"
        users = {u["id"]: u for u in rows["users"]}
        assert users["u-a1"]["email"] == "real-a1@corp.com"
        assert users["u-a1"]["name"] == "Alice Real"
        assert users["u-a1"]["clerk_user_id"] == "user_a1"
        assert "u-a2" in users  # full membership of sampled orgs, not just authors

        # experiments are untouched (incl. public sharing flags)
        exps = {e["id"]: e for e in rows["experiments"]}
        assert set(exps) == {"exp-a", "exp-b", "exp-mine"}  # deleted one excluded
        assert exps["exp-a"]["is_public"] is True
        assert exps["exp-a"]["public_token"] == "tok-a"
        assert exps["exp-a"]["org_id"] == "org-a"  # no remap

        # both same-named tasks survive (different orgs, like prod)
        tasks = {t["id"]: t for t in rows["tasks"]}
        assert {"task-dup-a", "task-dup-b"} <= set(tasks)
        assert tasks["task-solo"]["user"] == "real-a1@corp.com"  # no scrubbing
        # the only mutation: in-flight normalized so previews never run work
        assert tasks["task-run"]["status"] == "FAILED"
        trials = {t["id"]: t for t in rows["trials"]}
        assert trials["tr-running"]["status"] == "FAILED"

        # self-references deferred to the linkage pass
        assert ("tasks", "task-solo", "current_version_id", "ver-solo-2") in s1[
            "linkage"
        ]
        assert (
            "trials",
            "tr-superseded",
            "superseded_by_trial_id",
            "tr-running",
        ) in s1["linkage"]

        jobs = {j["id"]: j for j in rows["worker_jobs"]}
        assert "wj-live" not in jobs  # only terminal jobs imported
        assert jobs["wj-child"]["parent_job_id"] is None  # parent not sampled

        assert [s["id"] for s in rows["skills"]] == ["sk-a"]
        assert [f["id"] for f in rows["skill_files"]] == ["skf-a1"]
        assert [d["id"] for d in rows["documents"]] == ["doc-1"]

        # non-deleted tags for the sampled orgs (org-a + org-b); MERGED kept so
        # the page exercises its own state filtering, soft-deleted excluded
        tags = {t["id"]: t for t in rows["tags"]}
        assert set(tags) == {"t-smoke", "t-flaky", "t-bonly", "t-merged"}
        assert "t-del" not in tags  # deleted_at IS NOT NULL
        assert tags["t-smoke"]["key"] == "smoke"
        assert tags["t-smoke"]["owner_user_id"] == "u-a1"
        # the merged tag keeps its self-FK pointer (target was sampled)
        assert tags["t-merged"]["merged_into_id"] == "t-smoke"
        # only DIRECT/ACTIVE assignments onto sampled targets, one per scope
        tas = {a["id"]: a for a in rows["tag_assignments"]}
        assert set(tas) == {"ta-task", "ta-ver", "ta-exp", "ta-bonly"}
        assert tas["ta-task"]["scope"] == "TASK"
        assert tas["ta-ver"]["scope"] == "VERSION"
        assert tas["ta-exp"]["scope"] == "EXPERIMENT"
        assert tas["ta-task"]["assigned_by_user_id"] == "u-a1"
        # excluded: experiment-propagated, removed, and unsampled-target rows
        assert "ta-living" not in tas  # source != DIRECT
        assert "ta-removed" not in tas  # state != ACTIVE
        assert "ta-deltarget" not in tas  # target exp is deleted/unsampled
    finally:
        await src.dispose()


async def test_bulk_batches_load_and_fallback_yields_single_rows():
    """>_UPSERT_BATCH rows exercise the multi-row path end to end, and a
    batch tripping a secondary unique falls back row-by-row so only the
    colliding row yields to the pre-existing one."""
    src = await _make_source_db()
    # widen the source: 1,200 extra terminal trials on exp-a
    t = Base.metadata.tables
    async with src.begin() as c:
        await c.execute(
            t["trials"].insert(),
            [
                {
                    "id": f"tr-bulk-{i:04d}",
                    "name": f"tr-bulk-{i:04d}",
                    "task_id": "task-solo",
                    "task_version_id": "ver-solo-2",
                    "experiment_id": "exp-a",
                    "org_id": "org-a",
                    "agent": "claude",
                    "provider": "anthropic",
                    "queue_key": "q",
                    "timeout_minutes": 30,
                    "environment": "modal",
                    "harbor_config": {},
                    "status": "SUCCESS",
                    "origin": "oddish",
                    "result": {"reward": 0},
                    "superseded_by_trial_id": None,
                }
                for i in range(1200)
            ],
        )
    engine = create_async_engine(URL)
    try:
        import preview_seed as ps

        orig_cap = ps.SAMPLE_TRIALS_PER_EXPERIMENT
        ps.SAMPLE_TRIALS_PER_EXPERIMENT = 5000
        try:
            sampled = await ps.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        finally:
            ps.SAMPLE_TRIALS_PER_EXPERIMENT = orig_cap
        assert len(sampled["rows"]["trials"]) >= 1200

        await _reset_target(engine)
        # a reviewer-made task already holds a sampled CHILDLESS task's
        # (org, name) -- colliding a task that has trials would correctly
        # cascade-yield those trials too, which is not what we exercise here
        async with engine.begin() as c:
            await c.execute(
                text(
                    "insert into organizations (id, name, slug, plan, settings,"
                    " is_active, created_at, updated_at) values ('org-a', 'Real A',"
                    " 'real-a', 'free', '{}'::jsonb, true, now(), now())"
                )
            )
            await c.execute(
                text(
                    'insert into tasks (id, name, org_id, "user", priority,'
                    " status, task_path, tags, run_analysis, run_probe,"
                    " created_at, updated_at) values ('reviewer-made', 'dup-task',"
                    " 'org-a', 'r@corp.com', 'LOW', 'PENDING', 'p/m', '{}'::jsonb,"
                    " false, false, now(), now())"
                )
            )
        await ps.seed(engine, sampled=sampled)

        # all bulk rows landed
        assert (
            await _count(
                engine, "select count(*) from trials where id like 'tr-bulk-%'"
            )
            >= 1200
        )
        # only the colliding task yielded; its siblings landed
        assert (
            await _count(engine, "select count(*) from tasks where id='task-dup-a'")
            == 0
        )
        assert (
            await _count(engine, "select count(*) from tasks where id='reviewer-made'")
            == 1
        )
        assert (
            await _count(engine, "select count(*) from tasks where id='task-solo'") == 1
        )
    finally:
        await engine.dispose()
        await src.dispose()


async def test_trials_per_experiment_cap_bounds_the_draw(monkeypatch):
    src = await _make_source_db()
    try:
        t = Base.metadata.tables
        async with src.begin() as c:
            await c.execute(
                t["trials"].insert(),
                [
                    {
                        "id": f"tr-cap-{i:04d}",
                        "name": f"tr-cap-{i:04d}",
                        "task_id": "task-solo",
                        "task_version_id": "ver-solo-2",
                        "experiment_id": "exp-a",
                        "org_id": "org-a",
                        "agent": "claude",
                        "provider": "anthropic",
                        "queue_key": "q",
                        "timeout_minutes": 30,
                        "environment": "modal",
                        "harbor_config": {},
                        "status": "SUCCESS",
                        "origin": "oddish",
                        "result": {"reward": 0},
                        "superseded_by_trial_id": None,
                    }
                    for i in range(20)
                ],
            )
        monkeypatch.setattr(preview_seed, "SAMPLE_TRIALS_PER_EXPERIMENT", 3)
        s = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        exp_a_trials = [t for t in s["rows"]["trials"] if t["experiment_id"] == "exp-a"]
        assert len(exp_a_trials) == 3
    finally:
        await src.dispose()


async def test_per_owner_anchor_guarantees_mine_view_data(monkeypatch):
    """With the global anchors disabled, every distinct experiment owner is
    still represented in the draw -- the guarantee behind the dashboard
    "Mine" view showing data for every member in previews."""
    src = await _make_source_db()
    try:
        monkeypatch.setattr(preview_seed, "SAMPLE_RECENT_EXPERIMENTS", 0)
        monkeypatch.setattr(preview_seed, "SAMPLE_RANDOM_EXPERIMENTS", 0)
        s = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        owners = {e["owner_user_id"] for e in s["rows"]["experiments"]}
        assert owners == {"u-a1", "u-a2", "u-b1"}
    finally:
        await src.dispose()


async def test_seed_loads_subset_reconciles_drift_and_keeps_reviewer_data():
    src = await _make_source_db()
    engine = create_async_engine(URL)
    try:
        sampled = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        await _reset_target(engine)
        await preview_seed.seed(engine, sampled=sampled)

        # auth path: the real org row with its real clerk mapping is present
        assert (
            await _count(
                engine,
                "select count(*) from organizations"
                " where id='org-a' and clerk_org_id='org_real_a'",
            )
            == 1
        )
        # members are the real users
        assert (
            await _count(
                engine,
                "select count(*) from users where org_id='org-a'"
                " and email like '%@corp.com'",
            )
            == 2
        )
        # linkage applied; JSONB intact
        assert (
            await _count(
                engine,
                "select count(*) from tasks where id='task-solo'"
                " and current_version_id='ver-solo-2'",
            )
            == 1
        )
        assert (
            await _count(
                engine,
                "select count(*) from trials where id='tr-superseded'"
                " and superseded_by_trial_id='tr-running'",
            )
            == 1
        )
        assert (
            await _count(
                engine,
                "select count(*) from trials where id='tr-ok'"
                " and (result->>'reward')::int = 1",
            )
            == 1
        )

        # idempotent
        before = await _count(engine, "select count(*) from tasks")
        await preview_seed.seed(engine, sampled=sampled)
        assert await _count(engine, "select count(*) from tasks") == before

        # reviewer-created data (made in the preview by hand) must survive
        async with engine.begin() as c:
            await c.execute(
                text(
                    "insert into users (id, org_id, email, role, name, is_active,"
                    " created_at, updated_at) values ('jit-rev', 'org-a',"
                    " 'reviewer@corp.com', 'admin', 'Reviewer', true, now(), now())"
                )
            )
            await c.execute(
                text(
                    "insert into tasks (id, name, org_id, created_by_user_id,"
                    ' "user", priority, status, task_path, tags, run_analysis,'
                    " run_probe, created_at, updated_at) values ('manual-1',"
                    " 'reviewer-made', 'org-a', 'jit-rev', 'reviewer@corp.com',"
                    " 'LOW', 'PENDING', 'p/manual', '{}'::jsonb, false, false,"
                    " now(), now())"
                )
            )

        # prod drift: everything belonging to exp-b drops out of the draw
        drifted = {
            "rows": {
                name: [
                    r
                    for r in rows
                    if r.get("experiment_id") != "exp-b"
                    and r.get("id") not in ("exp-b", "task-dup-b", "ver-dup-b")
                    and r.get("task_id") != "task-dup-b"
                ]
                for name, rows in sampled["rows"].items()
            },
            "linkage": sampled["linkage"],
        }
        await preview_seed.seed(engine, sampled=drifted)
        assert (
            await _count(engine, "select count(*) from experiments where id='exp-b'")
            == 0
        )
        assert (
            await _count(engine, "select count(*) from tasks where id='task-dup-b'")
            == 0
        )
        assert (
            await _count(engine, "select count(*) from experiments where id='exp-a'")
            == 1
        )
        # reviewer data untouched by reconcile
        assert (
            await _count(engine, "select count(*) from tasks where id='manual-1'") == 1
        )
        assert (
            await _count(engine, "select count(*) from users where id='jit-rev'") == 1
        )

        # outage tolerance: sampled=None leaves everything alone
        await preview_seed.seed(engine, sampled=None)
        assert (
            await _count(engine, "select count(*) from experiments where id='exp-a'")
            == 1
        )
    finally:
        await engine.dispose()
        await src.dispose()


async def test_reseed_skips_unchanged_rows_and_writes_deltas():
    """Re-seeding an identical draw must not physically rewrite rows (ctid
    stable -- no index churn / dead tuples on re-pushes), while a changed
    row in the draw still lands."""
    src = await _make_source_db()
    engine = create_async_engine(URL)
    try:
        sampled = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        await _reset_target(engine)
        await preview_seed.seed(engine, sampled=sampled)

        async def ctid(task_id):
            async with engine.connect() as c:
                return (
                    await c.execute(
                        text(f"select ctid::text from tasks where id='{task_id}'")
                    )
                ).scalar_one()

        before_solo, before_dup = await ctid("task-solo"), await ctid("task-dup-a")
        await preview_seed.seed(engine, sampled=sampled)  # identical draw
        assert await ctid("task-solo") == before_solo  # incl. linkage no-op
        assert await ctid("task-dup-a") == before_dup

        for t in sampled["rows"]["tasks"]:
            if t["id"] == "task-dup-a":
                t["task_path"] = "p/changed"
        await preview_seed.seed(engine, sampled=sampled)
        assert (
            await _count(
                engine,
                "select count(*) from tasks where id='task-dup-a'"
                " and task_path='p/changed'",
            )
            == 1
        )
        assert await ctid("task-dup-a") != before_dup  # the delta was written
    finally:
        await engine.dispose()
        await src.dispose()


async def test_seed_cleans_legacy_fixtures_and_yields_to_jit_conflicts():
    src = await _make_source_db()
    engine = create_async_engine(URL)
    try:
        sampled = await preview_seed.sample_prod_subset(src, sample_key=SAMPLE_KEY)
        await _reset_target(engine)
        # a reused branch may carry artifacts of earlier seed versions ...
        async with engine.begin() as c:
            await c.execute(
                text(
                    "insert into organizations (id, name, slug, plan, settings,"
                    " is_active, created_at, updated_at) values"
                    " ('seed-org', 'Preview Org', 'preview-org', 'free',"
                    " '{}'::jsonb, true, now(), now()),"
                    " ('org-a', 'Real A', 'real-a', 'free', '{}'::jsonb, true,"
                    " now(), now())"
                )
            )
            await c.execute(
                text(
                    "insert into users (id, org_id, email, role, name, is_active,"
                    " created_at, updated_at) values"
                    " ('seed-usr-owner', 'seed-org', 'owner@preview.local',"
                    "  'admin', 'Preview Owner', true, now(), now()),"
                    " ('anon-1', 'seed-org', 'user-x@preview.local', 'member',"
                    "  'Prod User x', true, now(), now()),"
                    # ... and a JIT-provisioned reviewer holding a real user's
                    # unique identity under a different primary key
                    " ('jit-1', 'org-a', 'real-a1@corp.com', 'admin', 'Reviewer',"
                    "  true, now(), now())"
                )
            )

        await preview_seed.seed(engine, sampled=sampled)

        # legacy fixtures and anonymized users are gone
        assert (
            await _count(
                engine, "select count(*) from organizations where id='seed-org'"
            )
            == 0
        )
        assert (
            await _count(
                engine, "select count(*) from users where email like '%@preview.local'"
            )
            == 0
        )
        # the conflicting import yielded: one row holds that email, the JIT one
        assert (
            await _count(
                engine, "select count(*) from users where email='real-a1@corp.com'"
            )
            == 1
        )
        assert await _count(engine, "select count(*) from users where id='jit-1'") == 1
        # the rest of the subset still loaded
        assert (
            await _count(engine, "select count(*) from experiments where id='exp-a'")
            == 1
        )
        assert await _count(engine, "select count(*) from users where id='u-a2'") == 1
    finally:
        await engine.dispose()
        await src.dispose()
