"""The preview bootstrap must rebuild a cached schema when its inputs change.

Regression for the stale-trust bug: a preview branch that gained a column after
its first prep (a merge adding a migration, or a re-pointed migration chain) kept
its stale cached schema -- alembic was already at head, so
``alembic upgrade head`` was a no-op, the new column was never created, and every
read of it 500'd. The trust marker folds in the model-graph fingerprint (any
table/column/index change) AND the migration-content fingerprint (any migration
file edit), so the schema is rebuilt from a fresh prod snapshot whenever either
changes. The orchestration tests pin the rebuild step order (snapshot before any
branch write; trust marker last) and the trusted path's retry-then-heal
semantics.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fingerprint_is_deterministic():
    mod = _load_bootstrap()
    m = sa.MetaData()
    sa.Table("trials", m, sa.Column("id", sa.String), sa.Column("status", sa.String))
    assert mod._fingerprint_metadata(m) == mod._fingerprint_metadata(m)


def test_adding_a_column_changes_the_fingerprint():
    mod = _load_bootstrap()
    before = sa.MetaData()
    sa.Table(
        "trials", before, sa.Column("id", sa.String), sa.Column("status", sa.String)
    )
    after = sa.MetaData()
    sa.Table(
        "trials",
        after,
        sa.Column("id", sa.String),
        sa.Column("status", sa.String),
        sa.Column("harbor_sha", sa.String),
    )
    assert mod._fingerprint_metadata(before) != mod._fingerprint_metadata(after)


def test_adding_an_index_changes_the_fingerprint():
    # An index-only model change must invalidate the cached schema: only the
    # fingerprint and the model-schema assert ever see model-declared indexes.
    mod = _load_bootstrap()
    before = sa.MetaData()
    sa.Table(
        "worker_jobs",
        before,
        sa.Column("id", sa.String),
        sa.Column("kind", sa.String),
        sa.Column("subject_id", sa.String),
    )
    after = sa.MetaData()
    t = sa.Table(
        "worker_jobs",
        after,
        sa.Column("id", sa.String),
        sa.Column("kind", sa.String),
        sa.Column("subject_id", sa.String),
    )
    sa.Index("uq_worker_jobs_tag_project_active", t.c.kind, t.c.subject_id, unique=True)
    assert mod._fingerprint_metadata(before) != mod._fingerprint_metadata(after)


def test_trust_marker_folds_in_both_fingerprints(monkeypatch):
    mod = _load_bootstrap()
    monkeypatch.setattr(mod, "_model_fingerprint", lambda: "deadbeef")
    monkeypatch.setattr(mod, "_migration_fingerprint", lambda: "cafef00d")
    assert mod._trust_marker() == f"{mod.SCHEMA_MARKER}:deadbeef:cafef00d"
    # The bare pre-fix marker must no longer be accepted as trusted.
    assert mod._trust_marker() != mod.SCHEMA_MARKER


def test_migration_fingerprint_hashes_both_stacks_and_ignores_pycache(
    monkeypatch, tmp_path
):
    mod = _load_bootstrap()
    stack_a = tmp_path / "oddish"
    stack_b = tmp_path / "backend"
    (stack_a / "alembic" / "versions").mkdir(parents=True)
    (stack_b / "alembic" / "versions").mkdir(parents=True)
    (stack_a / "alembic" / "versions" / "aaa_one.py").write_text("revision = 'aaa'\n")
    (stack_b / "alembic" / "versions" / "bbb_two.py").write_text("revision = 'bbb'\n")
    monkeypatch.setattr(mod, "STACKS", (stack_a, stack_b))

    first = mod._migration_fingerprint()
    assert first == mod._migration_fingerprint()

    # Editing a migration's CONTENT in either stack changes the hash.
    (stack_b / "alembic" / "versions" / "bbb_two.py").write_text(
        "revision = 'bbb'\n# edited in place\n"
    )
    edited = mod._migration_fingerprint()
    assert edited != first

    # Compiled cache noise must not affect it.
    pycache = stack_a / "alembic" / "versions" / "__pycache__"
    pycache.mkdir()
    (pycache / "aaa_one.cpython-313.py").write_text("compiled noise")
    assert mod._migration_fingerprint() == edited


def test_libpq_url_strips_asyncpg_driver():
    mod = _load_bootstrap()
    assert (
        mod._libpq_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert mod._libpq_url("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_session_pooler_url_reroutes_transaction_pooler():
    # pg_dump/psql set session-level state; on the shared transaction pooler
    # (6543) that poisons pooled backends for later clients, so they must go
    # through the session pooler (5432) instead.
    mod = _load_bootstrap()
    assert (
        mod._session_pooler_url("postgresql+asyncpg://u:p@pooler.host:6543/postgres")
        == "postgresql://u:p@pooler.host:5432/postgres"
    )
    # Already-session or port-less URLs pass through untouched.
    assert (
        mod._session_pooler_url("postgresql://u:p@pooler.host:5432/postgres")
        == "postgresql://u:p@pooler.host:5432/postgres"
    )
    assert mod._session_pooler_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_filter_schema_sql_drops_only_create_schema_public():
    mod = _load_bootstrap()
    sql = (
        "SET search_path = public;\n"
        "CREATE SCHEMA public;\n"
        "  CREATE SCHEMA public;  \n"
        "CREATE TABLE trials (id text);\n"
    )
    filtered = mod._filter_schema_sql(sql)
    assert "CREATE SCHEMA public;" not in filtered
    assert "SET search_path = public;" in filtered
    assert "CREATE TABLE trials (id text);" in filtered
    assert filtered.endswith("\n")


def test_pg_dump_cmd_is_schema_only_public_and_session_pooler():
    mod = _load_bootstrap()
    cmd = mod._pg_dump_schema_cmd("postgresql+asyncpg://u:p@h:6543/db")
    assert cmd[0] == "pg_dump"
    for flag in (
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--schema=public",
    ):
        assert flag in cmd
    assert cmd[-1] == "postgresql://u:p@h:5432/db"


def test_psql_restore_cmd_is_single_transaction_and_session_pooler():
    mod = _load_bootstrap()
    cmd = mod._psql_restore_cmd("postgresql+asyncpg://u:p@h:6543/db")
    assert cmd[0] == "psql"
    for flag in ("--no-psqlrc", "--quiet", "--single-transaction"):
        assert flag in cmd
    assert "ON_ERROR_STOP=1" in cmd
    assert cmd[cmd.index("ON_ERROR_STOP=1") - 1] == "-v"
    assert cmd[-1] == "postgresql://u:p@h:5432/db"


def test_source_db_url_required_and_normalized(monkeypatch):
    mod = _load_bootstrap()
    monkeypatch.delenv("PREVIEW_SAMPLE_SOURCE_DB_URL", raising=False)
    with pytest.raises(RuntimeError):
        mod._source_db_url()
    monkeypatch.setenv("PREVIEW_SAMPLE_SOURCE_DB_URL", "postgresql://u:p@h/db")
    assert mod._source_db_url() == "postgresql+asyncpg://u:p@h/db"


def _orchestration_mod(monkeypatch, tmp_path, *, trusted, upgrade_failures=0):
    """Load the bootstrap with main()'s collaborators stubbed for orchestration tests.

    ``upgrade_failures`` = how many leading ``_upgrade_head`` calls raise before
    the stub starts succeeding (simulates a partially-applied autocommit
    migration that is / is not retry-safe).
    """
    mod = _load_bootstrap()
    calls: list[str] = []
    sentinel = tmp_path / "sentinel"
    monkeypatch.setenv("ODDISH_DATABASE_URL", "postgresql://stub")
    monkeypatch.setenv("SCHEMA_REBUILT_FILE", str(sentinel))

    async def _trusted(url):
        return trusted

    remaining = {"failures": upgrade_failures}

    def _upgrade(project):
        calls.append(f"upgrade:{project.name}")
        if remaining["failures"] > 0:
            remaining["failures"] -= 1
            raise RuntimeError("upgrade blew up")

    async def _assert_ok(url):
        calls.append("assert")

    def _rebuild(url):
        calls.append("rebuild")

    monkeypatch.setattr(mod, "_schema_trusted", _trusted)
    monkeypatch.setattr(mod, "_upgrade_head", _upgrade)
    monkeypatch.setattr(mod, "_assert_model_schema", _assert_ok)
    monkeypatch.setattr(mod, "_rebuild", _rebuild)
    return mod, calls, sentinel


def test_trusted_happy_path_upgrades_in_prod_order(monkeypatch, tmp_path):
    mod, calls, sentinel = _orchestration_mod(monkeypatch, tmp_path, trusted=True)
    mod.main()
    assert calls == ["upgrade:oddish", "upgrade:backend", "assert"]
    assert not sentinel.exists()


def test_trusted_retry_succeeds_without_heal(monkeypatch, tmp_path):
    # One failure then success = a retry-safe migration, prod's re-trigger
    # convention: no rebuild, no sentinel.
    mod, calls, sentinel = _orchestration_mod(
        monkeypatch, tmp_path, trusted=True, upgrade_failures=1
    )
    mod.main()
    assert "rebuild" not in calls
    assert calls.count("upgrade:oddish") == 2  # first attempt + retry
    assert not sentinel.exists()


def test_trusted_retry_failure_heals_once(monkeypatch, tmp_path):
    mod, calls, sentinel = _orchestration_mod(
        monkeypatch, tmp_path, trusted=True, upgrade_failures=99
    )
    mod.main()
    assert calls.count("rebuild") == 1
    assert sentinel.read_text() == "healed"


def test_trusted_assert_failure_heals_once(monkeypatch, tmp_path):
    mod, calls, sentinel = _orchestration_mod(monkeypatch, tmp_path, trusted=True)

    async def _assert_broken(url):
        calls.append("assert")
        raise RuntimeError("model column missing")

    monkeypatch.setattr(mod, "_assert_model_schema", _assert_broken)
    mod.main()
    assert calls.count("rebuild") == 1
    assert sentinel.read_text() == "healed"


def test_untrusted_rebuilds_and_writes_sentinel(monkeypatch, tmp_path):
    mod, calls, sentinel = _orchestration_mod(monkeypatch, tmp_path, trusted=False)
    mod.main()
    assert calls == ["rebuild"]
    assert sentinel.read_text() == "1"


def test_rebuild_failure_propagates_without_second_heal(monkeypatch, tmp_path):
    mod, calls, sentinel = _orchestration_mod(monkeypatch, tmp_path, trusted=False)

    def _rebuild_broken(url):
        calls.append("rebuild")
        raise RuntimeError("the PR's migration is genuinely broken")

    monkeypatch.setattr(mod, "_rebuild", _rebuild_broken)
    with pytest.raises(RuntimeError):
        mod.main()
    assert calls == ["rebuild"]
    assert not sentinel.exists()


def _rebuild_mod(monkeypatch, version_sequence):
    """Load the bootstrap with _rebuild's collaborators stubbed as recorders.

    ``version_sequence`` = successive results of the prod pointer reads, so the
    snapshot consistency bracket (read -> dump -> read) can be driven through
    match, retry, and give-up shapes.
    """
    mod = _load_bootstrap()
    calls: list[str] = []
    versions = iter(version_sequence)

    monkeypatch.setattr(mod, "_dump_prod_schema", lambda: calls.append("dump") or "sql")

    async def _fetch():
        calls.append("fetch_versions")
        return next(versions)

    async def _reset(url):
        calls.append("reset")

    async def _write(url, versions):
        calls.append("write_versions")

    async def _assert_ok(url):
        calls.append("assert")

    async def _trust(url):
        calls.append("trust")

    monkeypatch.setattr(mod, "_fetch_prod_alembic_versions", _fetch)
    monkeypatch.setattr(mod, "_reset_schema", _reset)
    monkeypatch.setattr(mod, "_restore_schema", lambda url, sql: calls.append("restore"))
    monkeypatch.setattr(mod, "_write_alembic_versions", _write)
    monkeypatch.setattr(mod, "_run_seed", lambda: calls.append("seed"))
    monkeypatch.setattr(
        mod, "_upgrade_head", lambda project: calls.append(f"upgrade:{project.name}")
    )
    monkeypatch.setattr(mod, "_assert_model_schema", _assert_ok)
    monkeypatch.setattr(mod, "_mark_trusted", _trust)
    return mod, calls


V1 = {"alembic_version_oddish": "x", "alembic_version_backend": "y"}
V2 = {"alembic_version_oddish": "z", "alembic_version_backend": "y"}
V3 = {"alembic_version_oddish": "w", "alembic_version_backend": "y"}


def test_rebuild_snapshots_prod_before_touching_the_branch(monkeypatch):
    # The step order is the safety property: the pointer bracket + dump happen
    # before any branch write, the seed runs before the upgrade (migrations
    # execute against data), and the trust marker is written last.
    mod, calls = _rebuild_mod(monkeypatch, [V1, V1])
    mod._rebuild("postgresql://stub")
    assert calls == [
        "fetch_versions",
        "dump",
        "fetch_versions",
        "reset",
        "restore",
        "write_versions",
        "seed",
        "upgrade:oddish",
        "upgrade:backend",
        "assert",
        "trust",
    ]


def test_rebuild_retries_snapshot_when_prod_migrates_mid_dump(monkeypatch):
    # Pointers moved during the first dump (older schema + newer pointers
    # would make upgrade head silently skip a migration) -> fresh dump, whose
    # bracket matches -> proceed.
    mod, calls = _rebuild_mod(monkeypatch, [V1, V2, V2, V2])
    mod._rebuild("postgresql://stub")
    assert calls[:6] == [
        "fetch_versions",
        "dump",
        "fetch_versions",
        "fetch_versions",
        "dump",
        "fetch_versions",
    ]
    assert calls[6] == "reset"
    assert calls[-1] == "trust"


def test_rebuild_fails_loud_when_prod_keeps_migrating(monkeypatch):
    # Both snapshot attempts bracket a pointer change -> loud failure with the
    # branch untouched (no reset/restore/seed ever recorded).
    mod, calls = _rebuild_mod(monkeypatch, [V1, V2, V2, V3])
    with pytest.raises(RuntimeError, match="migrated during two consecutive"):
        mod._rebuild("postgresql://stub")
    assert "reset" not in calls
    assert "restore" not in calls
    assert "seed" not in calls


def test_orchestration_helpers_are_asyncio_run_compatible():
    # main() drives the async collaborators via asyncio.run; a plain-sync stub
    # would mask signature drift, so pin that the real ones are coroutines.
    mod = _load_bootstrap()
    for fn in (
        mod._schema_trusted,
        mod._assert_model_schema,
        mod._fetch_prod_alembic_versions,
        mod._mark_trusted,
        mod._reset_schema,
    ):
        assert asyncio.iscoroutinefunction(fn), fn.__name__
