from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import daytona
from daytona import SandboxState

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings
from oddish.runtime.backends.daytona import (
    DaytonaBackend,
    reap_stale_daytona_sandboxes,
)


def test_daytona_backend_name_matches_environment_value() -> None:
    assert DaytonaBackend().name == "daytona"


def test_daytona_capabilities_are_cpu_only_no_private_registry() -> None:
    caps = DaytonaBackend().capabilities()
    assert caps.gpu is None
    assert caps.private_registry_pull is False
    assert caps.cold_start == "seconds"


def test_daytona_env_kwargs_inject_autostop_autodelete_ephemeral() -> None:
    merged = DaytonaBackend().harbor_env_kwargs({})
    assert merged["auto_stop_interval_mins"] == settings.daytona_auto_stop_interval_mins
    assert (
        merged["auto_delete_interval_mins"]
        == settings.daytona_auto_delete_interval_mins
    )
    assert merged["ephemeral"] == settings.daytona_ephemeral
    expected = datetime.now(timezone.utc) + timedelta(
        minutes=settings.daytona_sandbox_expiry_minutes
    )
    assert abs(int(merged["labels"]["oddish.expires_at"]) - expected.timestamp()) < 2


def test_daytona_env_kwargs_caller_overrides_win() -> None:
    merged = DaytonaBackend().harbor_env_kwargs(
        {
            "ephemeral": False,
            "auto_labels": False,
            "labels": {
                "task": "x",
                "oddish.managed": "false",
                "oddish.expires_at": "0",
            },
            "extra": "x",
        }
    )
    assert merged["ephemeral"] is False
    assert merged["auto_labels"] is True
    assert merged["labels"]["task"] == "x"
    assert merged["labels"]["oddish.managed"] == "true"
    assert int(merged["labels"]["oddish.expires_at"]) > 0
    assert merged["extra"] == "x"


def _fake_daytona(monkeypatch, sandboxes, fail_ids=()):
    calls = SimpleNamespace(deleted=[], closed=False, query=None, terminal_query=None)
    terminal = {SandboxState.ERROR, SandboxState.BUILD_FAILED}

    async def list_sandboxes(**kwargs):
        calls.terminal_query = kwargs
        return SimpleNamespace(
            items=[sandbox for sandbox in sandboxes if sandbox.state in terminal]
        )

    class Client:
        _sandbox_api = SimpleNamespace(list_sandboxes=list_sandboxes)

        async def list(self, query):
            calls.query = query
            for sandbox in sandboxes:
                yield sandbox

        async def get(self, sandbox_id, request_timeout=None):
            return next(sandbox for sandbox in sandboxes if sandbox.id == sandbox_id)

        async def delete(self, sandbox, timeout=60):
            if sandbox.id in fail_ids:
                raise RuntimeError("delete failed")
            calls.deleted.append(sandbox.id)

        async def close(self):
            calls.closed = True

    monkeypatch.setattr(daytona, "AsyncDaytona", Client)
    return calls


def test_reap_stale_daytona_sandboxes(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=30)).isoformat()
    recent = (now - timedelta(minutes=5)).isoformat()
    expired = str(int((now - timedelta(minutes=1)).timestamp()))
    future = str(int((now + timedelta(minutes=1)).timestamp()))
    labels = {"harbor.managed": "true", "oddish.managed": "true"}
    sandbox = partial(
        SimpleNamespace,
        labels=labels,
        created_at=old,
        updated_at=None,
    )
    sandboxes = [
        sandbox(id="bad", state=SandboxState.ERROR),
        sandbox(id="error", state=SandboxState.ERROR),
        sandbox(id="failed", state=SandboxState.BUILD_FAILED),
        sandbox(id="recent", state=SandboxState.ERROR, created_at=recent),
        sandbox(id="updated", state=SandboxState.ERROR, updated_at=recent),
        sandbox(
            id="unmanaged",
            state=SandboxState.ERROR,
            labels={"harbor.managed": "false", "oddish.managed": "true"},
        ),
        sandbox(
            id="not-oddish",
            state=SandboxState.ERROR,
            labels={"harbor.managed": "true"},
        ),
        sandbox(
            id="expired-started",
            state=SandboxState.STARTED,
            labels=labels | {"oddish.expires_at": expired},
        ),
        sandbox(
            id="started",
            state=SandboxState.STARTED,
            labels=labels | {"oddish.expires_at": future},
        ),
        *[
            sandbox(
                id=state.value,
                state=state,
                labels=labels | {"oddish.expires_at": expired},
            )
            for state in (SandboxState.DESTROYED, SandboxState.DESTROYING)
        ],
    ]
    calls = _fake_daytona(monkeypatch, sandboxes, {"bad"})

    assert asyncio.run(reap_stale_daytona_sandboxes()) == 3
    assert calls.deleted == ["error", "failed", "expired-started"]
    assert calls.closed is True
    assert calls.query.labels == labels
    assert calls.terminal_query["include_errored_deleted"] is True
    assert calls.terminal_query["created_at_before"] < now
    assert len(calls.terminal_query["states"]) == 2
    assert calls.query.states is None
    assert calls.terminal_query["limit"] == calls.query.limit == 50
