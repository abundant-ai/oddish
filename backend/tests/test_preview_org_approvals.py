"""Real PostgreSQL checks of preview approval ownership and deployment gating."""

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine

from preview_org_approvals import read_approval_decisions, sync_approval_decisions
import preview_seed

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="local PostgreSQL required"),
]


@pytest_asyncio.fixture
async def database():
    admin = create_async_engine(URL, isolation_level="AUTOCOMMIT")
    name = "approval_test_" + uuid.uuid4().hex[:10]
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(admin.url.set(database=name))
    async with engine.begin() as conn:
        await conn.execute(
            text("""
            CREATE TABLE organizations (
                id text PRIMARY KEY, name text NOT NULL, slug text UNIQUE NOT NULL,
                clerk_org_id text UNIQUE, plan text NOT NULL DEFAULT 'free',
                settings jsonb DEFAULT '{}', is_active boolean NOT NULL DEFAULT true,
                deleted_at timestamptz, execution_enabled boolean NOT NULL DEFAULT false,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        )
    yield engine
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    await admin.dispose()


def decision(id, approved=True):
    return {
        "id": id,
        "name": id,
        "slug": id,
        "clerk_org_id": "clerk_" + id,
        "approved": approved,
    }


async def insert_org(engine, id, *, approved=False, active=True, clerk_id=None):
    async with engine.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO organizations (id,name,slug,clerk_org_id,execution_enabled,is_active)
            VALUES (:id,:id,:id,:clerk,:approved,:active)
        """),
            {
                "id": id,
                "clerk": clerk_id or "clerk_" + id,
                "approved": approved,
                "active": active,
            },
        )


async def flags(engine):
    async with engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text("SELECT id,execution_enabled FROM organizations")
                )
            ).all()
        )


async def test_sync_includes_unsampled_orgs_and_revokes_unknown_and_revoked(database):
    await insert_org(database, "existing", clerk_id="clerk_approved")
    await insert_org(database, "revoked", approved=True)
    await insert_org(database, "unknown", approved=True)
    rows = [decision("approved"), decision("unsampled"), decision("revoked", False)]
    result = await sync_approval_decisions(
        database, rows, required_clerk_ids=("clerk_approved",)
    )
    assert result == {"approved": 2, "inserted": 1, "updated": 4}
    assert await flags(database) == {
        "existing": True,
        "unsampled": True,
        "revoked": False,
        "unknown": False,
    }
    assert (await sync_approval_decisions(database, rows, required_clerk_ids=()))[
        "updated"
    ] == 0
    async with database.connect() as conn:
        approved = await conn.scalar(
            text("SELECT execution_enabled FROM organizations WHERE id='existing'")
        )
    assert approved is True


async def test_required_org_missing_fails_without_changes(database):
    await insert_org(database, "unknown", approved=True)
    with pytest.raises(RuntimeError, match="not approved in production"):
        await sync_approval_decisions(
            database, [], required_clerk_ids=("clerk_required",)
        )
    assert await flags(database) == {"unknown": True}


async def test_legacy_personal_org_uses_exact_database_id(database):
    row = decision("personal")
    row["clerk_org_id"] = None
    await sync_approval_decisions(database, [row], required_clerk_ids=())
    assert await flags(database) == {"personal": True}
    row["approved"] = False
    await sync_approval_decisions(database, [row], required_clerk_ids=())
    assert await flags(database) == {"personal": False}


async def test_identity_collision_fails_without_approving_wrong_org(database):
    await insert_org(database, "collision", clerk_id="different_clerk")
    with pytest.raises(RuntimeError, match="inaccessible in preview"):
        await sync_approval_decisions(
            database, [decision("collision")], required_clerk_ids=()
        )
    assert await flags(database) == {"collision": False}


async def test_inactive_preview_org_rolls_back_entire_sync(database):
    await insert_org(database, "disabled", active=False)
    await insert_org(database, "unknown", approved=True)
    with pytest.raises(RuntimeError, match="inaccessible in preview"):
        await sync_approval_decisions(
            database, [decision("disabled"), decision("new")], required_clerk_ids=()
        )
    assert await flags(database) == {"disabled": False, "unknown": True}


async def test_source_uses_approval_and_active_state_and_requires_schema(database):
    await insert_org(database, "approved", approved=True)
    await insert_org(database, "inactive", approved=True, active=False)
    rows = await read_approval_decisions(database)
    assert {r["id"]: r["approved"] for r in rows} == {
        "approved": True,
        "inactive": False,
    }
    async with database.begin() as conn:
        await conn.execute(
            text("ALTER TABLE organizations DROP COLUMN execution_enabled")
        )
    with pytest.raises(RuntimeError, match="not initialized"):
        await read_approval_decisions(database)


@pytest.mark.parametrize("fallback", [False, True])
async def test_sample_copy_cannot_grant_or_revoke_approval(
    database, monkeypatch, fallback
):
    await insert_org(database, "existing", approved=True)
    metadata = MetaData()
    async with database.connect() as conn:
        await conn.run_sync(metadata.reflect)
    if fallback:

        async def fail_copy(*args):
            raise RuntimeError("exercise batch fallback")

        monkeypatch.setattr(preview_seed, "_load_table_copy_merge", fail_copy)
    rows = [
        {
            "id": id,
            "name": id,
            "slug": id,
            "clerk_org_id": "clerk_" + id,
            "execution_enabled": id == "new",
            "is_active": True,
            "plan": "free",
            "settings": {},
            "created_at": preview_seed.SEED_EPOCH,
            "updated_at": preview_seed.SEED_EPOCH,
        }
        for id in ("existing", "new")
    ]
    await preview_seed._load_table(database, metadata.tables["organizations"], rows)
    assert await flags(database) == {"existing": True, "new": False}
