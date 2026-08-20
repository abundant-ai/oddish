"""A preview branch keeps its own API keys, and never another environment's.

Two properties, pulling in opposite directions:

* PERSISTENCE -- a rebuild must not invalidate the key a developer just created
  from the preview dashboard. Rebuilds are not rare: the trust marker is
  written last, so any cancelled run leaves the branch untrusted and the next
  push rebuilds it. Before this, three pushes in a row silently destroyed the
  key three times.
* ISOLATION -- an API key is a credential minted against one environment. It
  must never be copied from prod into a preview, nor from one preview into
  another. Persistence is achieved by reading the rows from and writing them
  back to the SAME branch database, never by sampling them from elsewhere.
"""

import importlib.util
import os
from pathlib import Path

import preview_seed
import pytest


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestIsolation:
    """API keys never cross an environment boundary."""

    def test_api_keys_are_never_sampled_from_prod(self):
        assert "api_keys" in preview_seed._NEVER_SAMPLED_TABLES

    def test_api_keys_are_not_reconciled_from_the_sample(self):
        assert "api_keys" not in preview_seed._RECONCILED_TABLES

    def test_sampling_a_credential_table_raises(self):
        """A future edit that pulls api_keys into the sample must fail loudly.

        A broken preview seed is recoverable; a leaked credential is not.
        """
        with pytest.raises(RuntimeError, match="refusing to copy credential"):
            preview_seed._assert_no_forbidden_tables(
                {"experiments": [{"id": "e1"}], "api_keys": [{"id": "k1"}]}
            )

    def test_an_ordinary_sample_passes(self):
        preview_seed._assert_no_forbidden_tables(
            {"experiments": [{"id": "e1"}], "trials": [{"id": "t1"}]}
        )


class TestPersistence:
    """The branch's own keys survive its rebuild."""

    def test_api_keys_are_preserved_across_a_rebuild(self):
        assert "api_keys" in _load_bootstrap()._PRESERVED_TABLES

    def test_capture_precedes_the_drop_and_restore_follows_the_upgrade(self):
        """Order is the whole correctness argument.

        Capture must read the table before DROP SCHEMA destroys it, and the
        restore must run after ``upgrade head`` so the table has its final
        shape. Trust is still marked last.
        """
        source = (
            Path(__file__).resolve().parents[2]
            / ".github/scripts/preview/bootstrap_preview_db.py"
        ).read_text()
        body = source.split("def _rebuild(", 1)[1].split("\ndef ", 1)[0]

        order = [
            step
            for step in (
                "_capture_preserved_rows",
                "_reset_schema",
                "_run_seed",
                "_upgrade_head",
                "_restore_preserved_rows",
                "_mark_trusted",
            )
            if step in body
        ]
        assert order == [
            "_capture_preserved_rows",
            "_reset_schema",
            "_run_seed",
            "_upgrade_head",
            "_restore_preserved_rows",
            "_mark_trusted",
        ], f"unexpected rebuild step order: {order}"


URL = os.environ.get("ODDISH_DATABASE_URL")
# asyncio_mode is "strict", so async tests need the marker explicitly.
pytestmark_db = pytest.mark.skipif(
    not URL, reason="set ODDISH_DATABASE_URL to an empty Postgres to run these"
)

_DDL = """
CREATE TYPE apikeyscope AS ENUM ('full','tasks','read_only');
CREATE TABLE public.users (
    id varchar(64) PRIMARY KEY,
    email varchar(255) NOT NULL
);
CREATE TABLE public.organizations (
    id varchar(64) PRIMARY KEY,
    name varchar(255) NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.api_keys (
    id varchar(64) PRIMARY KEY,
    org_id varchar(64) NOT NULL,
    name varchar(255) NOT NULL,
    key_prefix varchar(16) NOT NULL,
    key_hash varchar(128) NOT NULL UNIQUE,
    scope apikeyscope NOT NULL,
    created_by_user_id varchar(64),
    is_active boolean NOT NULL DEFAULT true,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Real constraint: backend/alembic a1b2c3d4e5f6 adds
    -- fk_api_keys_created_by_user_id, and the later oddish drop migration
    -- removes api_keys_created_by_user_id_fkey -- a DIFFERENT name -- so this
    -- survives a rebuild.
    CONSTRAINT fk_api_keys_created_by_user_id
        FOREIGN KEY (created_by_user_id) REFERENCES public.users(id)
);
"""


async def _fresh_public(engine):
    """Stand in for the rebuild: drop public and recreate it empty."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        for statement in _DDL.strip().split(";\n"):
            if statement.strip():
                await conn.execute(text(statement))


async def _read(engine, table):
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(text(f"SELECT * FROM public.{table} ORDER BY id"))
        return [dict(row) for row in result.mappings()]


@pytest.mark.asyncio
@pytestmark_db
class TestPersistenceAgainstPostgres:
    """The rows really do survive, not just the call order.

    Structural tests cannot catch a restore that runs and inserts nothing, so
    these drive the real functions against a real database.
    """

    async def _seeded(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        # statement_cache_size=0 mirrors the production engine: DDL between
        # statements invalidates asyncpg's cached plans otherwise.
        engine = create_async_engine(URL, connect_args={"statement_cache_size": 0})
        await _fresh_public(engine)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS preview_preserved CASCADE"))
            await conn.execute(
                text("INSERT INTO public.users VALUES ('u1', 'dev@preview.local')")
            )
            await conn.execute(
                text(
                    "INSERT INTO public.organizations (id, name)"
                    " VALUES ('org-local', 'Local Personal Org')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO public.api_keys"
                    " (id, org_id, name, key_prefix, key_hash, scope,"
                    "  created_by_user_id)"
                    " VALUES ('k1', 'org-local', 'gke', 'ok_pr-1', 'h1', 'full',"
                    "  'u1')"
                )
            )
        return engine

    async def test_rows_survive_a_rebuild_unchanged(self):
        mod = _load_bootstrap()
        engine = await self._seeded()
        before_keys = await _read(engine, "api_keys")
        before_orgs = await _read(engine, "organizations")

        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        assert await mod._restore_preserved_rows(URL) is True

        assert await _read(engine, "api_keys") == before_keys
        # The organization matters as much as the key: auth rejects a key whose
        # org row is missing, so a key restored alone would still not work.
        assert await _read(engine, "organizations") == before_orgs
        await engine.dispose()

    async def test_rows_survive_a_run_cancelled_before_the_restore(self):
        """The workflow uses cancel-in-progress, so this is the common case.

        A second push kills the run between the drop and the restore. The stash
        lives in the database, not in that process, so the next run recovers.
        """
        mod = _load_bootstrap()
        engine = await self._seeded()
        before_keys = await _read(engine, "api_keys")

        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        # ... the run is cancelled here; no restore happens.

        # Next run: capture sees an empty public table, restore uses the stash.
        await mod._capture_preserved_rows(URL)
        await mod._restore_preserved_rows(URL)

        assert await _read(engine, "api_keys") == before_keys
        await engine.dispose()

    async def test_restore_twice_does_not_duplicate(self):
        mod = _load_bootstrap()
        engine = await self._seeded()

        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        await mod._restore_preserved_rows(URL)
        await mod._restore_preserved_rows(URL)

        assert len(await _read(engine, "api_keys")) == 1
        await engine.dispose()

    async def test_a_missing_stash_is_not_an_error(self):
        from sqlalchemy import text

        mod = _load_bootstrap()
        engine = await self._seeded()
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS preview_preserved CASCADE"))
        await _fresh_public(engine)
        await mod._restore_preserved_rows(URL)
        assert await _read(engine, "api_keys") == []
        await engine.dispose()


@pytest.mark.asyncio
@pytestmark_db
class TestRestoreSurvivesBadRows:
    """One row that cannot be written must not take the others with it.

    A single set-at-a-time INSERT aborts the whole transaction, so one stale
    payload would lose every preserved key. These are the cases raised in
    review against this branch.
    """

    async def _seeded(self):
        return await TestPersistenceAgainstPostgres()._seeded()

    async def test_key_survives_when_its_creator_is_gone(self):
        """api_keys.created_by_user_id keeps a real FK on a rebuilt schema.

        If the creator cannot be restored, the key is retried with that
        column cleared: losing the attribution beats losing the key.
        """
        from sqlalchemy import text

        mod = _load_bootstrap()
        engine = await self._seeded()
        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM preview_preserved.rows WHERE table_name = 'users'")
            )

        assert await mod._restore_preserved_rows(URL) is True
        keys = await _read(engine, "api_keys")
        assert len(keys) == 1
        assert keys[0]["created_by_user_id"] is None
        await engine.dispose()

    async def test_unrestorable_row_leaves_the_schema_untrusted(self):
        """A NOT NULL column added after the payload was stashed.

        The row cannot be written, so the restore reports failure and the
        caller must not mark the schema trusted -- otherwise the next push
        takes the upgrade path, never restores, and the loss is permanent.
        """
        from sqlalchemy import text

        mod = _load_bootstrap()
        engine = await self._seeded()
        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE public.api_keys"
                    " ADD COLUMN tier varchar(16) NOT NULL DEFAULT 'x'"
                )
            )
            await conn.execute(
                text("ALTER TABLE public.api_keys ALTER COLUMN tier DROP DEFAULT")
            )

        assert await mod._restore_preserved_rows(URL) is False
        # The other tables still came back: failure is per row, not per run.
        assert len(await _read(engine, "organizations")) == 1
        await engine.dispose()

    async def test_key_is_skipped_when_its_organization_is_missing(self):
        """verify_api_key refuses a key whose org row is absent, so restoring
        it would look repaired while failing every request."""
        from sqlalchemy import text

        mod = _load_bootstrap()
        engine = await self._seeded()
        await mod._capture_preserved_rows(URL)
        await _fresh_public(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM preview_preserved.rows"
                    " WHERE table_name = 'organizations'"
                )
            )

        assert await mod._restore_preserved_rows(URL) is False
        assert await _read(engine, "api_keys") == []
        await engine.dispose()


class TestCaptureGuardsAgainstProduction:
    """Capture is now the first database write in a rebuild.

    It creates the stash schema before _reset_schema runs its guard, so a
    mispointed ODDISH_DATABASE_URL would copy credentials into production
    before the drop refused.
    """

    def test_capture_asserts_the_preview_branch_before_writing(self):
        source = (
            Path(__file__).resolve().parents[2]
            / ".github/scripts/preview/bootstrap_preview_db.py"
        ).read_text()
        body = source.split("async def _capture_preserved_rows(", 1)[1]
        body = body.split("\nasync def _restore_preserved_rows", 1)[0]
        assert "_assert_preview_branch(url)" in body, (
            "capture is the first database write in a rebuild; it must assert "
            "the target is a preview branch before creating the stash schema"
        )
        assert body.index("_assert_preview_branch(url)") < body.index(
            "CREATE SCHEMA IF NOT EXISTS"
        ), "the guard must run before the first write"
