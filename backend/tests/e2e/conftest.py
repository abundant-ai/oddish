from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

E2E_ENABLED = os.environ.get("ODDISH_E2E", "").strip() in {"1", "true", "yes", "on"}
DB_URL = os.environ.get("ODDISH_DATABASE_URL")

_BACKEND_DIR = Path(__file__).resolve().parents[2]

_STRIP_PREFIXES = ("ODDISH_", "CLERK_", "MODAL_")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_DUMMY_S3_ENV = {
    "ODDISH_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
    "ODDISH_S3_ACCESS_KEY": "dummy",
    "ODDISH_S3_SECRET_KEY": "dummy",
    "ODDISH_S3_BUCKET": "dummy",
    "ODDISH_S3_REGION": "us-east-1",
}

_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active "
    "ON worker_jobs (kind, subject_table, subject_id) "
    "WHERE kind = 'TAG_PROJECT' "
    "AND status IN ('QUEUED', 'RETRYING') "
    "AND subject_id IS NOT NULL"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_STRIP_PREFIXES)}


def _require_local_db() -> None:
    host = make_url(DB_URL).host
    if host not in _LOCAL_HOSTS:
        raise RuntimeError(
            f"e2e suite refuses non-local database host {host!r}; "
            "point ODDISH_DATABASE_URL at a throwaway localhost container"
        )


@pytest_asyncio.fixture(scope="session")
async def schema() -> None:
    _require_local_db()
    import models  # noqa: F401
    from sqlalchemy.ext.asyncio import create_async_engine

    from oddish.db.models import Base

    engine = create_async_engine(DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
            await conn.execute(text(_INDEX_DDL))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def live_server(schema, tmp_path_factory) -> str:
    port = _free_port()
    env = {
        **_clean_env(),
        **_DUMMY_S3_ENV,
        "ODDISH_DATABASE_URL": DB_URL,
        "CLERK_DOMAIN": "dummy",
        "PORT": str(port),
    }
    # Server output goes to a file, never a PIPE: nothing drains a pipe while
    # the server runs, so enough log volume would fill the buffer and block
    # the child mid-test.
    log_path = tmp_path_factory.mktemp("e2e-server") / "server.log"
    with log_path.open("wb") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "serve.py"],
            cwd=str(_BACKEND_DIR),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(proc, base, log_path)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _wait_ready(proc: subprocess.Popen, base: str, log_path: Path) -> None:
    deadline = time.monotonic() + 30
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = log_path.read_text(errors="replace")
            raise RuntimeError(f"server exited early (code {proc.returncode}):\n{out}")
        try:
            r = httpx.get(f"{base}/public/experiments", timeout=2.0)
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.3)
    out = log_path.read_text(errors="replace")
    raise RuntimeError(
        f"server not ready at {base} within 30s (last: {last})\nserver log:\n{out}"
    )


@pytest_asyncio.fixture
async def seeded(schema):
    from models import OrganizationModel
    from oddish.core.api_keys import create_api_key
    from oddish.db import get_session

    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_e2e_{suffix}"
    task_id = f"task_e2e_{suffix}"
    key_model, raw_key = create_api_key(org_id=org_id, name=f"e2e-{suffix}")

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(key_model)
        await session.execute(
            text(
                "insert into tasks "
                '(id,name,org_id,"user",priority,status,task_path,tags,'
                "effective_tag_ids,current_version_tag_ids,run_analysis,run_probe,"
                "created_at,updated_at) "
                "values (:id,:id,:org,'u','LOW','COMPLETED','p','{}'::jsonb,"
                "'{}'::text[],'{}'::text[],false,false,now(),now())"
            ),
            {"id": task_id, "org": org_id},
        )

    try:
        yield {
            "suffix": suffix,
            "org_id": org_id,
            "task_id": task_id,
            "api_key": raw_key,
        }
    finally:
        await _purge(org_id, task_id, key_model.id)


async def _purge(org_id: str, task_id: str, api_key_id: str) -> None:
    from models import APIKeyModel, OrganizationModel
    from oddish.db import TaskModel, TrialModel, get_session

    async with get_session() as session:
        await session.execute(
            text("delete from worker_jobs where subject_id in "
                 "(select id from trials where task_id = :t)"),
            {"t": task_id},
        )
        await session.execute(
            text("delete from worker_jobs where subject_id = :t"), {"t": task_id}
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
        )
        await session.execute(
            text("delete from experiments where org_id = :o"), {"o": org_id}
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            APIKeyModel.__table__.delete().where(APIKeyModel.id == api_key_id)
        )
        await session.execute(
            OrganizationModel.__table__.delete().where(OrganizationModel.id == org_id)
        )


def cli(base_url: str, api_key: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        **_clean_env(),
        "ODDISH_API_URL": base_url,
        "ODDISH_API_KEY": api_key,
    }
    return subprocess.run(
        ["uv", "run", "oddish", *args],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
