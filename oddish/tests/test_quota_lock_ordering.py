"""Org -> payer advisory lock ordering regression tests.

The LOCK ORDER contract (see acquire_payer_lock's docstring) is: org lock ->
payer lock -> row locks, everywhere a quota-gated transaction takes locks. Two
transactions acquiring these in different orders deadlock ABBA, so the ordering
must hold on every path: the single sweep core, the batch core (pre-loop AND
per-item), and retry via admit_trials. These tests spy on the acquire functions
(no DB needed) and, at the SQL level, on a recording session, so a future
reordering fails loudly here instead of as a production deadlock.

Also pinned: SHADOW / OFF acquire neither lock (a dark feature must not
serialize submissions), and with no org cap configured the org lock is skipped
under ENFORCE while the payer lock still fires.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.core.endpoints.sweep as sweep_mod  # noqa: E402
import oddish.core.quota_admission as quota_admission  # noqa: E402
import oddish.core.sweeps as sweeps_mod  # noqa: E402
import oddish.queue as queue_mod  # noqa: E402
from oddish.config import QuotaMode, settings  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.core.endpoints.sweep import (  # noqa: E402
    create_task_sweep_batch_core,
    create_task_sweep_core,
)
from oddish.core.harbor_source import HarborSourceError  # noqa: E402
from oddish.core.quota_admission import acquire_org_lock, admit_trials  # noqa: E402
from oddish.db import TaskStatus, TrialModel, TrialStatus  # noqa: E402


def _install_lock_spies(monkeypatch, events: list[tuple]) -> None:
    """Replace both acquire functions with order-recording spies.

    Both the sweep cores (lazy ``from oddish.core.quota_admission import ...``
    at call time) and admit_trials (module-global references) resolve these
    through the quota_admission module, so patching the module attributes
    covers every call site.
    """

    async def fake_org(session, org_id):
        events.append(("org", org_id))

    async def fake_payer(session, org_id, payer):
        events.append(("payer", org_id, payer))

    monkeypatch.setattr(quota_admission, "acquire_org_lock", fake_org)
    monkeypatch.setattr(quota_admission, "acquire_payer_lock", fake_payer)


def _stop_after_locks(monkeypatch) -> None:
    """Abort create_task_sweep_core right after the lock acquisition.

    ``resolve_and_gate_harbor`` is the first thing the sweep core runs after
    the locks (idempotency is skipped when no store is passed), so raising
    there surfaces as the 422 the core maps HarborSourceError to.
    """

    def fake_gate(harbor, *, settings):
        raise HarborSourceError("stop after the locks")

    monkeypatch.setattr(sweep_mod, "resolve_and_gate_harbor", fake_gate)


class _FakeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def begin_nested(self):
        return _FakeSavepoint()


# --- single sweep core: org lock before payer lock ------------------------------


@pytest.mark.asyncio
async def test_single_sweep_core_takes_org_lock_before_payer_lock(monkeypatch):
    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)
    _stop_after_locks(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await create_task_sweep_core(
            _FakeSession(),
            submission=SimpleNamespace(harbor=None),
            org_id="org-1",
            billed_user_id="amy",
        )

    assert raised.value.status_code == 422
    assert events == [("org", "org-1"), ("payer", "org-1", "amy")]


# --- doomed (unattributed-under-ENFORCE) submissions take NO locks ---------------


@pytest.mark.asyncio
async def test_single_sweep_core_unattributed_enforce_rejects_before_any_lock(
    monkeypatch,
):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50"))
    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)
    _stop_after_locks(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await create_task_sweep_core(
            _FakeSession(),
            submission=SimpleNamespace(harbor=None),
            org_id="org-1",
            billed_user_id=None,
        )

    # 403 Unattributed, raised BEFORE the org/payer locks (and before harbor
    # resolution -- _stop_after_locks' 422 was never reached).
    assert raised.value.status_code == 403
    assert events == []


@pytest.mark.asyncio
async def test_batch_unattributed_items_pre_fail_without_org_lock(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50"))
    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)
    _stop_after_locks(monkeypatch)
    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", lambda _s: None)

    async def resolve_none(session, submission):
        return None

    # All items unattributed -> every item pre-fails 403 and the batch never
    # takes the org lock at all.
    submissions = [SimpleNamespace(harbor=None) for _ in range(2)]
    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=submissions,
        org_id="org-1",
        resolve_billed_user_id=resolve_none,
    )
    assert [r.status_code for r in results] == [403, 403]
    assert events == []

    # Mixed batch: the unattributed item pre-fails 403 without contributing to
    # lock acquisition; the attributed item still locks org -> payer.
    submissions = [SimpleNamespace(harbor=None) for _ in range(2)]
    payers = {id(submissions[0]): None, id(submissions[1]): "amy"}

    async def resolve_mixed(session, submission):
        return payers[id(submission)]

    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=submissions,
        org_id="org-1",
        resolve_billed_user_id=resolve_mixed,
    )
    assert [r.status_code for r in results] == [403, 422]
    assert events == [
        ("org", "org-1"),
        ("payer", "org-1", "amy"),
        # Per-item (via create_task_sweep_core) for the surviving item only.
        ("org", "org-1"),
        ("payer", "org-1", "amy"),
    ]


# --- batch core: org lock first, sorted payers, then per-item org -> payer -------


@pytest.mark.asyncio
async def test_batch_takes_org_lock_before_payer_locks_pre_loop_and_per_item(
    monkeypatch,
):
    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)
    _stop_after_locks(monkeypatch)
    monkeypatch.setattr(sweeps_mod, "validate_sweep_submission", lambda _s: None)

    submissions = [SimpleNamespace(harbor=None) for _ in range(3)]
    payers = {
        id(submissions[0]): "zed",
        id(submissions[1]): "amy",
        id(submissions[2]): "zed",
    }

    async def resolve(session, submission):
        return payers[id(submission)]

    results = await create_task_sweep_batch_core(
        _FakeSession(),
        submissions=submissions,
        org_id="org-1",
        resolve_billed_user_id=resolve,
    )

    assert [r.status_code for r in results] == [422, 422, 422]
    assert events == [
        # Pre-loop: ONE org lock, then the distinct payer locks in sorted order.
        ("org", "org-1"),
        ("payer", "org-1", "amy"),
        ("payer", "org-1", "zed"),
        # Per item (via create_task_sweep_core): org before payer, every time.
        ("org", "org-1"),
        ("payer", "org-1", "zed"),
        ("org", "org-1"),
        ("payer", "org-1", "amy"),
        ("org", "org-1"),
        ("payer", "org-1", "zed"),
    ]


# --- retry path: admit_trials locks org -> payer before the supersede CAS --------


class _Result:
    def __init__(self, scalar=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def __iter__(self):
        return iter(())


class _RecordingTrial:
    def __init__(self, events, *, billed_user_id):
        self._events = events
        self._superseded_by_trial_id = None
        self.id = "task-1-0"
        self.name = "task-1-0"
        self.task_id = "task-1"
        self.task_version_id = "task-1-v1"
        self.experiment_id = "exp-1"
        self.org_id = "org-1"
        self.billed_user_id = billed_user_id
        self.agent = "codex"
        self.provider = "openai"
        self.queue_key = "openai/gpt-5"
        self.model = "gpt-5"
        self.timeout_minutes = None
        self.environment = None
        self.harbor_config = None
        self.is_probe = False
        self.max_attempts = 6
        self.attempts = 1
        self.status = TrialStatus.FAILED
        self.error_message = "boom"
        self.harbor_stage = None
        self.finished_at = None
        self.current_worker_id = None
        self.current_queue_slot = None
        self.cost_usd = None
        self.deleted_at = None

    @property
    def superseded_by_trial_id(self):
        return self._superseded_by_trial_id

    @superseded_by_trial_id.setter
    def superseded_by_trial_id(self, value):
        self._events.append(("supersede", value))
        self._superseded_by_trial_id = value


class _RecordingSession:
    """Just enough session for retry_trial_core + the real admit_trials sums.

    Generic ``scalar`` returns None, which makes every quota SUM read as $0 and
    every override read fall back to the settings default -- so admission
    passes and the retry proceeds to the supersede CAS.
    """

    def __init__(self, *, trial, task, events):
        self.trial = trial
        self.task = task
        self.events = events

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.events.append(("execute", sql))
        if "UPDATE trials" in sql and "superseded_by_trial_id IS NULL" in sql:
            self.trial.superseded_by_trial_id = params["new_trial_id"]
            return _Result(rowcount=1)
        return _Result(scalar=self.trial)

    async def scalar(self, statement, params=None):
        self.events.append(("scalar", str(statement)))
        return None

    async def get(self, model, key, **kwargs):
        self.events.append(("get", key))
        return self.trial if model is TrialModel else self.task

    def add(self, obj):
        self.events.append(("add", obj.id))

    async def flush(self):
        self.events.append(("flush", None))

    def expire(self, _obj):
        self.events.append(("expire", None))

    async def commit(self):
        self.events.append(("commit", None))


@pytest.mark.asyncio
async def test_retry_takes_org_lock_then_payer_lock_before_supersede(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("1000"))

    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)

    trial = _RecordingTrial(events, billed_user_id="amy")
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.COMPLETED, finished_at=None
    )
    session = _RecordingSession(trial=trial, task=task, events=events)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **_kwargs):
        return None

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    result = await endpoints.retry_trial_core(
        session, trial_id=trial.id, org_id="org-1"
    )

    assert result["trial_id"] == "task-1-1"
    lock_events = [e for e in events if e[0] in ("org", "payer")]
    assert lock_events == [("org", "org-1"), ("payer", "org-1", "amy")]
    names = [e[0] for e in events]
    assert names.index("org") < names.index("payer") < names.index("supersede")


@pytest.mark.asyncio
async def test_retry_null_billed_still_admits_against_org_cap(monkeypatch):
    """A NULL-billed (legacy) retry must run admission: no Unattributed 403
    (the allow_unattributed carve-out), no payer lock (no payer), but the org
    lock IS taken and the org-wide check runs before the supersede."""
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("1000"))

    events: list[tuple] = []
    _install_lock_spies(monkeypatch, events)

    trial = _RecordingTrial(events, billed_user_id=None)
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.COMPLETED, finished_at=None
    )
    session = _RecordingSession(trial=trial, task=task, events=events)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **_kwargs):
        return None

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    result = await endpoints.retry_trial_core(
        session, trial_id=trial.id, org_id="org-1"
    )

    assert result["trial_id"] == "task-1-1"
    lock_events = [e for e in events if e[0] in ("org", "payer")]
    assert lock_events == [("org", "org-1")]  # org lock yes, payer lock no
    names = [e[0] for e in events]
    assert names.index("org") < names.index("supersede")


# --- SQL level: real acquire fns on a recording session --------------------------


class _LockRecordingSession:
    """Records execute() SQL; scalar() reads (override rows, SUMs) return None."""

    def __init__(self):
        self.executes: list[tuple[str, dict | None]] = []

    async def execute(self, statement, params=None):
        self.executes.append((str(statement), params))

    async def scalar(self, statement, params=None):
        return None

    def advisory_lock_keys(self) -> list[str]:
        return [
            params["k"]
            for sql, params in self.executes
            if "pg_advisory_xact_lock" in sql
        ]


@pytest.mark.asyncio
async def test_acquire_org_lock_escape_hatches(monkeypatch):
    recording = _LockRecordingSession()
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50"))

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    await acquire_org_lock(recording, "org-x")
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    await acquire_org_lock(recording, "org-x")
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    await acquire_org_lock(recording, None)
    assert recording.executes == []  # OFF / SHADOW / no org never lock

    # ENFORCE but no org cap configured anywhere -> the whole org must not be
    # serialized for a disabled feature.
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", None)
    await acquire_org_lock(recording, "org-x")
    assert recording.executes == []

    # ENFORCE with a configured cap -> the org key is locked.
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50"))
    await acquire_org_lock(recording, "org-x")
    assert recording.advisory_lock_keys() == ["quota-org:org-x"]


@pytest.mark.asyncio
async def test_admit_trials_shadow_and_off_take_no_advisory_locks(monkeypatch):
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("50"))

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    recording = _LockRecordingSession()
    await admit_trials(recording, "org-x", "user-x", count=1)
    assert recording.executes == []

    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    recording = _LockRecordingSession()
    await admit_trials(recording, "org-x", "user-x", count=1)
    assert recording.advisory_lock_keys() == []
    # Also for the unattributed-in-shadow path (which now runs the org check).
    recording = _LockRecordingSession()
    await admit_trials(recording, "org-x", None, count=1)
    assert recording.advisory_lock_keys() == []


@pytest.mark.asyncio
async def test_admit_trials_enforce_without_org_cap_takes_only_payer_lock(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", None)

    recording = _LockRecordingSession()
    await admit_trials(recording, "org-x", "user-x", count=1)

    assert recording.advisory_lock_keys() == ["quota:org-x:user-x"]


@pytest.mark.asyncio
async def test_admit_trials_enforce_with_org_cap_locks_org_then_payer(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "default_org_daily_quota_usd", Decimal("1000"))

    recording = _LockRecordingSession()
    await admit_trials(recording, "org-x", "user-x", count=1)

    assert recording.advisory_lock_keys() == [
        "quota-org:org-x",
        "quota:org-x:user-x",
    ]
