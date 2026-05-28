from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import endpoints
from oddish.db import ExperimentModel, TrialModel, TrialStatus
from oddish.schemas import ExperimentCombineRequest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _FakeCombineSession:
    """Async-session fake returning canned results in execution order.

    ``combine_experiments_core`` issues exactly three SELECTs (linked task
    ids, source trials, task metadata); every other DB helper it touches
    (link, index reservation, activity bump, experiment lookup) is
    monkeypatched out, so an ordered result list is enough.
    """

    def __init__(self, *, linked_task_ids, source_trials, task_meta_rows):
        self._results = [
            _FakeRowsResult([(tid,) for tid in linked_task_ids]),
            _FakeScalarsResult(source_trials),
            _FakeRowsResult(task_meta_rows),
        ]
        self.execute_calls = 0
        self.added: list[object] = []
        self.flush_calls = 0

    async def execute(self, _statement, *_args, **_kwargs):
        result = self._results[self.execute_calls]
        self.execute_calls += 1
        return result

    def add(self, obj):
        # Simulate the DB-side ``default=generate_id`` for the result
        # experiment row (Python defaults only fire on a real INSERT).
        if isinstance(obj, ExperimentModel) and obj.id is None:
            obj.id = "exp-result"
        self.added.append(obj)

    async def flush(self):
        self.flush_calls += 1


class _FakeStorage:
    def __init__(self, per_call=3):
        self.calls: list[tuple[str, str]] = []
        self._per_call = per_call

    async def copy_prefix(self, src_prefix, dst_prefix):
        self.calls.append((src_prefix, dst_prefix))
        return self._per_call


def _trial(trial_id, task_id, status, *, reward=None, org_id="org-1"):
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task_id,
        experiment_id="src",
        org_id=org_id,
        status=status,
        reward=reward,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
    )


def _patch_helpers(monkeypatch, *, experiments, storage, reserve):
    import oddish.experiment as experiment_mod
    import oddish.queue as queue_mod
    import oddish.db.storage as storage_mod

    async def _get_experiment(_session, identifier, _org_id=None):
        return experiments.get(identifier)

    link_calls: list[tuple[str, str]] = []

    async def _link(_session, *, task_id, experiment_id):
        link_calls.append((task_id, experiment_id))

    async def _reserve(_session, *, task_id):
        return reserve[task_id]

    bump_calls: list[object] = []

    async def _bump(_session, *, experiment_ids):
        bump_calls.append(experiment_ids)

    monkeypatch.setattr(queue_mod, "get_experiment_by_id_or_name", _get_experiment)
    monkeypatch.setattr(queue_mod, "_link_task_to_experiment", _link)
    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", _reserve)
    monkeypatch.setattr(queue_mod, "bump_experiment_last_activity", _bump)
    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: storage)
    monkeypatch.setattr(experiment_mod, "generate_experiment_name", lambda: "auto-name")
    return link_calls, bump_calls


# ---------------------------------------------------------------------------
# combine_experiments_core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combine_copies_terminal_trials_and_links_tasks(monkeypatch):
    experiments = {
        "exp-A": ExperimentModel(id="exp-A", name="alpha", org_id="org-1"),
        "exp-B": ExperimentModel(id="exp-B", name="beta", org_id="org-1"),
    }
    source_trials = [
        _trial("task-1-0", "task-1", TrialStatus.SUCCESS, reward=1.0),
        _trial("task-1-1", "task-1", TrialStatus.RUNNING),  # skipped (in-flight)
        _trial("task-2-0", "task-2", TrialStatus.FAILED, reward=0.0),
    ]
    storage = _FakeStorage(per_call=3)
    link_calls, bump_calls = _patch_helpers(
        monkeypatch,
        experiments=experiments,
        storage=storage,
        reserve={"task-1": 5, "task-2": 2},
    )

    session = _FakeCombineSession(
        linked_task_ids=["task-1", "task-2"],
        source_trials=source_trials,
        task_meta_rows=[
            ("task-1", "task-one", "org-1"),
            ("task-2", "task-two", "org-1"),
        ],
    )

    result = await endpoints.combine_experiments_core(
        session,
        source_experiment_ids=["exp-A", "exp-B"],
        name="combined",
        org_id="org-1",
        copy_artifacts=True,
    )

    assert result.id == "exp-result"
    assert result.name == "combined"
    assert result.source_experiment_ids == ["exp-A", "exp-B"]
    assert result.tasks_linked == 2
    assert result.trials_copied == 2
    assert result.trials_skipped == 1
    assert result.artifacts_copied == 6  # 2 copied trials * 3 objects each

    # Both tasks linked into the result experiment.
    assert link_calls == [("task-1", "exp-result"), ("task-2", "exp-result")]
    assert bump_calls == ["exp-result"]

    # New immutable trial rows under the same tasks, fresh ids, results kept.
    new_trials = [obj for obj in session.added if isinstance(obj, TrialModel)]
    assert [t.id for t in new_trials] == ["task-1-5", "task-2-2"]
    assert all(t.experiment_id == "exp-result" for t in new_trials)
    assert [t.status for t in new_trials] == [TrialStatus.SUCCESS, TrialStatus.FAILED]
    assert [t.reward for t in new_trials] == [1.0, 0.0]
    assert new_trials[0].name == "task-one-5"
    assert new_trials[0].idempotency_key == "combine:exp-result:task-1-0"
    # copy_artifacts=True => own prefix.
    assert new_trials[0].trial_s3_key == "tasks/task-1/trials/task-1-5/"

    # Server-side copies from each source prefix to the new prefix.
    assert storage.calls == [
        ("tasks/task-1/trials/task-1-0/", "tasks/task-1/trials/task-1-5/"),
        ("tasks/task-2/trials/task-2-0/", "tasks/task-2/trials/task-2-2/"),
    ]


@pytest.mark.asyncio
async def test_combine_shared_artifacts_skips_s3_copy(monkeypatch):
    experiments = {
        "exp-A": ExperimentModel(id="exp-A", name="alpha", org_id="org-1"),
        "exp-B": ExperimentModel(id="exp-B", name="beta", org_id="org-1"),
    }
    source_trials = [_trial("task-1-0", "task-1", TrialStatus.SUCCESS, reward=1.0)]
    storage = _FakeStorage()
    _patch_helpers(
        monkeypatch,
        experiments=experiments,
        storage=storage,
        reserve={"task-1": 1},
    )

    session = _FakeCombineSession(
        linked_task_ids=["task-1"],
        source_trials=source_trials,
        task_meta_rows=[("task-1", "task-one", "org-1")],
    )

    result = await endpoints.combine_experiments_core(
        session,
        source_experiment_ids=["exp-A", "exp-B"],
        org_id="org-1",
        copy_artifacts=False,
    )

    assert result.artifacts_copied == 0
    assert storage.calls == []  # no duplication when sharing artifacts

    new_trial = next(o for o in session.added if isinstance(o, TrialModel))
    # Shared mode points the copy at the source trial's resolved prefix.
    assert new_trial.trial_s3_key == "tasks/task-1/trials/task-1-0/"


@pytest.mark.asyncio
async def test_combine_generates_name_when_omitted(monkeypatch):
    experiments = {
        "exp-A": ExperimentModel(id="exp-A", name="alpha", org_id="org-1"),
        "exp-B": ExperimentModel(id="exp-B", name="beta", org_id="org-1"),
    }
    storage = _FakeStorage()
    _patch_helpers(monkeypatch, experiments=experiments, storage=storage, reserve={})

    session = _FakeCombineSession(
        linked_task_ids=[], source_trials=[], task_meta_rows=[]
    )

    result = await endpoints.combine_experiments_core(
        session,
        source_experiment_ids=["exp-A", "exp-B"],
        org_id="org-1",
    )

    assert result.name == "auto-name"
    assert result.trials_copied == 0
    assert result.tasks_linked == 0


@pytest.mark.asyncio
async def test_combine_requires_two_distinct_sources(monkeypatch):
    experiments = {"exp-A": ExperimentModel(id="exp-A", name="alpha", org_id="org-1")}
    storage = _FakeStorage()
    _patch_helpers(monkeypatch, experiments=experiments, storage=storage, reserve={})

    session = _FakeCombineSession(
        linked_task_ids=[], source_trials=[], task_meta_rows=[]
    )

    # Two ids that resolve to the *same* experiment collapse to one source.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await endpoints.combine_experiments_core(
            session,
            source_experiment_ids=["exp-A", "exp-A"],
            org_id="org-1",
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_combine_unknown_experiment_is_404(monkeypatch):
    experiments = {"exp-A": ExperimentModel(id="exp-A", name="alpha", org_id="org-1")}
    storage = _FakeStorage()
    _patch_helpers(monkeypatch, experiments=experiments, storage=storage, reserve={})

    session = _FakeCombineSession(
        linked_task_ids=[], source_trials=[], task_meta_rows=[]
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await endpoints.combine_experiments_core(
            session,
            source_experiment_ids=["exp-A", "missing"],
            org_id="org-1",
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


def test_request_dedupes_and_requires_two_sources():
    from pydantic import ValidationError

    req = ExperimentCombineRequest(
        source_experiment_ids=["a", " b ", "a", "b"], name="  result  "
    )
    assert req.source_experiment_ids == ["a", "b"]
    assert req.name == "result"
    assert req.copy_artifacts is True

    with pytest.raises(ValidationError):
        ExperimentCombineRequest(source_experiment_ids=["only-one"])


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def _find_combine_route(router_path: Path):
    source = router_path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(router_path))
    for node in module.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute) or func.attr != "post":
                continue
            if not decorator.args:
                continue
            first = decorator.args[0]
            if (
                isinstance(first, ast.Constant)
                and first.value == "/experiments/combine"
            ):
                return ast.get_source_segment(source, node)
    return None


def test_oss_server_exposes_combine_endpoint():
    server_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "oddish"
        / "server"
        / "__init__.py"
    )
    route_source = _find_combine_route(server_path)
    assert route_source is not None, "Expected POST /experiments/combine in OSS server"
    assert "combine_experiments_core(" in route_source


def test_backend_router_exposes_combine_endpoint():
    router_path = (
        Path(__file__).resolve().parents[2] / "backend" / "api" / "routers" / "tasks.py"
    )
    route_source = _find_combine_route(router_path)
    assert route_source is not None, "Expected POST /experiments/combine in backend"
    assert "combine_experiments_core(" in route_source
    assert "org_id=auth.org_id" in route_source
    assert "invalidate_dashboard_cache(" in route_source


# ---------------------------------------------------------------------------
# StorageClient.copy_prefix
# ---------------------------------------------------------------------------


class _FakePaginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, **_kwargs):
        keys = self._keys

        class _Aiter:
            def __aiter__(self_inner):
                self_inner._yielded = False
                return self_inner

            async def __anext__(self_inner):
                if self_inner._yielded:
                    raise StopAsyncIteration
                self_inner._yielded = True
                return {"Contents": [{"Key": k} for k in keys]}

        return _Aiter()


class _FakeS3Client:
    def __init__(self, keys):
        self._keys = keys
        self.copied: list[tuple[str, str]] = []

    def get_paginator(self, _name):
        return _FakePaginator(self._keys)

    async def copy_object(self, *, Bucket, Key, CopySource):  # noqa: N803
        self.copied.append((CopySource["Key"], Key))


@pytest.mark.asyncio
async def test_copy_prefix_swaps_prefix_and_skips_import_archive():
    from oddish.db.storage import StorageClient

    storage = StorageClient()
    storage._client = _FakeS3Client(
        keys=[
            "tasks/t/trials/t-0/result.json",
            "tasks/t/trials/t-0/agent/log.txt",
            "tasks/t/trials/t-0/.oddish-trial-import.tar.gz",  # staging, skipped
        ]
    )

    copied = await storage.copy_prefix("tasks/t/trials/t-0/", "tasks/t/trials/t-9/")

    assert copied == 2
    assert sorted(storage._client.copied) == [
        ("tasks/t/trials/t-0/agent/log.txt", "tasks/t/trials/t-9/agent/log.txt"),
        ("tasks/t/trials/t-0/result.json", "tasks/t/trials/t-9/result.json"),
    ]


@pytest.mark.asyncio
async def test_copy_prefix_noop_when_source_equals_destination():
    from oddish.db.storage import StorageClient

    storage = StorageClient()
    storage._client = _FakeS3Client(keys=["tasks/t/trials/t-0/result.json"])

    copied = await storage.copy_prefix("tasks/t/trials/t-0/", "tasks/t/trials/t-0/")

    assert copied == 0
    assert storage._client.copied == []
