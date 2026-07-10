"""Unit tests for the GKE orphan-pod sweeper decision logic.

The sweeper script lives under ``deploy/k8s/`` (it ships in its own minimal
image, independent of the Oddish package), so it is loaded here by file path
rather than imported as a package module.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "k8s" / "gke_orphan_sweeper.py"
)
_spec = importlib.util.spec_from_file_location("gke_orphan_sweeper", _MODULE_PATH)
assert _spec and _spec.loader
sweeper = importlib.util.module_from_spec(_spec)
# Register before exec so the module's own dataclasses can resolve their
# (string, due to ``from __future__ import annotations``) field annotations.
sys.modules[_spec.name] = sweeper
_spec.loader.exec_module(sweeper)

PodSnapshot = sweeper.PodSnapshot
LivenessView = sweeper.LivenessView
Verdict = sweeper.Verdict
decide_orphan_pods = sweeper.decide_orphan_pods

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=10)


def _pod(
    name: str,
    *,
    phase: str = "Running",
    age: timedelta = timedelta(hours=1),
    app: str | None = "sandbox",
) -> "sweeper.PodSnapshot":
    labels = {"session": name} if app is None else {"app": app, "session": name}
    return PodSnapshot(
        name=name,
        phase=phase,
        creation_time=NOW - age,
        labels=labels,
    )


def _liveness(
    *,
    live: set[str] | None = None,
    dead: set[str] | None = None,
    has_live_unlinked_worker: bool = False,
) -> "sweeper.LivenessView":
    return LivenessView(
        owned_live_pods=frozenset(live or set()),
        owned_dead_pods=frozenset(dead or set()),
        has_live_unlinked_worker=has_live_unlinked_worker,
    )


def _decide(pods, liveness):
    return decide_orphan_pods(pods, liveness, now=NOW, grace_period=GRACE)


def _verdict(pods, liveness, name: str) -> "sweeper.Verdict":
    plan = _decide(pods, liveness)
    return {pv.name: pv.verdict for pv in plan.verdicts}[name]


# --- the brief's required cases ------------------------------------------


def test_pre_hook_orphan_no_handle_is_deleted():
    # A pod with no owning worker_job at all (the worker died before/without
    # persisting a handle) and no live-unlinked worker to protect it: delete.
    pod = _pod("orphan", age=timedelta(hours=3))
    verdict = _verdict([pod], _liveness(), "orphan")
    assert verdict == Verdict.DELETE_ORPHAN


def test_live_pending_trial_is_never_deleted_even_when_old():
    # The core safety case: a legitimately Pending pod (long DWS wait) whose
    # worker_job is live must survive regardless of age.
    pod = _pod("pending-live", phase="Pending", age=timedelta(hours=12))
    verdict = _verdict([pod], _liveness(live={"pending-live"}), "pending-live")
    assert verdict == Verdict.KEEP_LIVE


def test_dead_worker_pending_pod_is_deleted():
    # A Pending pod whose owning worker_job is dead (terminal or stale) is an
    # orphan holding a ProvisioningRequest: delete it.
    pod = _pod("pending-dead", phase="Pending", age=timedelta(hours=2))
    verdict = _verdict([pod], _liveness(dead={"pending-dead"}), "pending-dead")
    assert verdict == Verdict.DELETE_DEAD_OWNER


def test_just_created_pod_within_grace_is_not_deleted():
    # Younger than the grace window: keep, even with no owner yet (its handle
    # / heartbeat may simply not be observable yet).
    pod = _pod("fresh", age=timedelta(minutes=2))
    verdict = _verdict([pod], _liveness(), "fresh")
    assert verdict == Verdict.KEEP_WITHIN_GRACE


def test_live_running_trial_is_not_deleted():
    pod = _pod("running-live", phase="Running", age=timedelta(hours=5))
    verdict = _verdict([pod], _liveness(live={"running-live"}), "running-live")
    assert verdict == Verdict.KEEP_LIVE


# --- the residual live-but-unlinked guard --------------------------------


def test_unlinked_old_pod_is_kept_when_a_live_unlinked_worker_exists():
    # The rare failed-handle-write case: a live trial's pod exists but its
    # worker_job never recorded external_id. We cannot tell it apart from a
    # true orphan, and a live-unlinked worker is present, so we must keep it.
    pod = _pod("maybe-live", phase="Pending", age=timedelta(hours=6))
    verdict = _verdict([pod], _liveness(has_live_unlinked_worker=True), "maybe-live")
    assert verdict == Verdict.KEEP_LIVE_UNLINKED_GUARD


def test_unlinked_old_pod_is_deleted_when_no_live_unlinked_worker():
    pod = _pod("truly-orphan", age=timedelta(hours=6))
    verdict = _verdict([pod], _liveness(has_live_unlinked_worker=False), "truly-orphan")
    assert verdict == Verdict.DELETE_ORPHAN


def test_live_ownership_beats_the_unlinked_guard():
    # A pod owned by a live worker is kept as KEEP_LIVE even if a separate
    # live-unlinked worker also exists.
    pod = _pod("owned", age=timedelta(hours=6))
    verdict = _verdict(
        [pod],
        _liveness(live={"owned"}, has_live_unlinked_worker=True),
        "owned",
    )
    assert verdict == Verdict.KEEP_LIVE


# --- ownership / status edges --------------------------------------------


def test_dead_owner_via_terminal_status_running_pod_deleted():
    pod = _pod("done", phase="Running", age=timedelta(hours=2))
    verdict = _verdict([pod], _liveness(dead={"done"}), "done")
    assert verdict == Verdict.DELETE_DEAD_OWNER


def test_live_precedence_when_pod_in_both_live_and_dead_sets():
    # Defensive: a name resolving to both a live and a dead worker_job is
    # treated as live (never delete a possibly-live pod).
    pod = _pod("ambiguous", age=timedelta(hours=2))
    verdict = _verdict(
        [pod], _liveness(live={"ambiguous"}, dead={"ambiguous"}), "ambiguous"
    )
    assert verdict == Verdict.KEEP_LIVE


def test_grace_boundary_is_inclusive_keep():
    # Age exactly equal to grace is kept (delete only when strictly older).
    pod = _pod("edge", age=GRACE)
    verdict = _verdict([pod], _liveness(), "edge")
    assert verdict == Verdict.KEEP_WITHIN_GRACE


def test_one_second_past_grace_orphan_is_deleted():
    pod = _pod("edge2", age=GRACE + timedelta(seconds=1))
    verdict = _verdict([pod], _liveness(), "edge2")
    assert verdict == Verdict.DELETE_ORPHAN


def test_missing_creation_time_is_kept():
    # If we cannot age a pod we must not delete it.
    pod = PodSnapshot(
        name="no-age", phase="Pending", creation_time=None, labels={"app": "sandbox"}
    )
    verdict = _verdict([pod], _liveness(), "no-age")
    assert verdict == Verdict.KEEP_WITHIN_GRACE


def test_non_sandbox_pod_is_kept():
    pod = _pod("not-sandbox", app=None, age=timedelta(hours=3))
    verdict = _verdict([pod], _liveness(), "not-sandbox")
    assert verdict == Verdict.KEEP_NOT_SANDBOX


def test_non_sandbox_pod_with_other_app_label_is_kept():
    pod = _pod("worker", app="oddish-worker", age=timedelta(hours=3))
    verdict = _verdict([pod], _liveness(), "worker")
    assert verdict == Verdict.KEEP_NOT_SANDBOX


# --- plan shape / aggregate helpers --------------------------------------


def test_empty_pod_list_yields_empty_plan():
    plan = _decide([], _liveness(has_live_unlinked_worker=True))
    assert plan.verdicts == ()
    assert plan.to_delete == []
    assert sum(plan.counts().values()) == 0


def test_to_delete_and_counts_over_a_mixed_batch():
    pods = [
        _pod("live", age=timedelta(hours=2)),
        _pod("dead", age=timedelta(hours=2)),
        _pod("orphan", age=timedelta(hours=2)),
        _pod("fresh", age=timedelta(minutes=1)),
    ]
    plan = _decide(pods, _liveness(live={"live"}, dead={"dead"}))
    assert set(plan.to_delete) == {"dead", "orphan"}
    counts = plan.counts()
    assert counts[Verdict.KEEP_LIVE.value] == 1
    assert counts[Verdict.DELETE_DEAD_OWNER.value] == 1
    assert counts[Verdict.DELETE_ORPHAN.value] == 1
    assert counts[Verdict.KEEP_WITHIN_GRACE.value] == 1


def test_every_pod_gets_exactly_one_verdict():
    pods = [_pod(f"p{i}", age=timedelta(hours=1)) for i in range(5)]
    plan = _decide(pods, _liveness())
    assert [pv.name for pv in plan.verdicts] == [f"p{i}" for i in range(5)]


# --- runner glue (fake cluster + fake db) --------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        self._conn.executed.append((sql, params))
        if "environment" in sql:  # the RUNNING-gke-handles guard query
            self._result = [(h,) for h in self._conn.running_gke_handles]
        else:  # the handle-linkage query
            self._conn.linkage_params = params
            self._result = list(self._conn.rows)

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, rows, has_live_unlinked_worker=False, running_gke_handles=None):
        # rows: list of (external_id, status, finished_at)
        self.rows = rows
        # Legacy knob: True means "one RUNNING gke trial with no handle".
        if running_gke_handles is None:
            running_gke_handles = [None] if has_live_unlinked_worker else []
        self.running_gke_handles = running_gke_handles
        self.executed: list = []
        self.linkage_params = None

    def cursor(self):
        return _FakeCursor(self)


class _FakeApiError(Exception):
    def __init__(self, status):
        super().__init__(f"api error {status}")
        self.status = status


class _FakePod:
    def __init__(self, name, phase="Running", age=timedelta(hours=1), app="sandbox"):
        labels = {"session": name}
        if app is not None:
            labels["app"] = app

        class _Meta:
            pass

        class _Status:
            pass

        self.metadata = _Meta()
        self.metadata.name = name
        self.metadata.uid = f"uid-{name}"
        self.metadata.creation_timestamp = NOW - age
        self.metadata.labels = labels
        self.status = _Status()
        self.status.phase = phase


class _FakeList:
    def __init__(self, items):
        self.items = items


class _FakeCoreV1:
    def __init__(self, pods, fail_delete=None, raise_status=None):
        self._pods = pods
        self.fail_delete = fail_delete or set()
        self.raise_status = raise_status or {}  # name -> http status to raise
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.delete_bodies: dict[str, object] = {}

    def list_namespaced_pod(self, namespace, label_selector=None):
        self.calls.append(f"list:{label_selector}")
        return _FakeList(list(self._pods))

    def delete_namespaced_pod(
        self, name, namespace, grace_period_seconds=None, body=None
    ):
        self.calls.append(f"delete:{name}:{grace_period_seconds}")
        self.delete_bodies[name] = body
        if name in self.raise_status:
            raise _FakeApiError(self.raise_status[name])
        if name in self.fail_delete:
            raise RuntimeError("boom")
        self.deleted.append(name)


def test_resolve_liveness_running_is_live_dead_is_terminal_live_wins():
    # Liveness is status-based (RUNNING = live), not heartbeat-based. A terminal
    # row whose finish is consistent with the pod's creation is a dead owner; a
    # row that both RUNNING and terminal reference resolves to live.
    pods = [
        _pod("live", age=timedelta(hours=2)),
        _pod("terminal", age=timedelta(hours=2)),
        _pod("retrying", age=timedelta(hours=2)),
        _pod("dup", age=timedelta(hours=2)),
    ]
    rows = [
        ("oddish-trials/live", "RUNNING", None),
        # finished after the pod was created -> genuine dead owner
        ("oddish-trials/terminal", "SUCCESS", NOW - timedelta(hours=1)),
        # non-terminal row still carrying a prior attempt's handle (no finish)
        ("oddish-trials/retrying", "RETRYING", None),
        ("oddish-trials/dup", "RUNNING", None),
        ("oddish-trials/dup", "FAILED", NOW - timedelta(hours=1)),
    ]
    conn = _FakeConn(rows, has_live_unlinked_worker=True)
    view = sweeper.resolve_liveness(conn, pods, namespace="oddish-trials")
    assert view.owned_live_pods == frozenset({"live", "dup"})
    assert view.owned_dead_pods == frozenset({"terminal", "retrying"})
    assert view.has_live_unlinked_worker is True


def test_resolve_liveness_ignores_stale_alias_handle():
    # Pod name reuse: a terminal worker_job that finished BEFORE this pod was
    # created cannot be its owner (the name was reused). The pod must not be
    # classed dead-owned on that stale row's account.
    pods = [_pod("reused", age=timedelta(minutes=20))]
    rows = [
        # old trial finished an hour ago; this pod was created 20 min ago
        ("oddish-trials/reused", "SUCCESS", NOW - timedelta(hours=1)),
    ]
    conn = _FakeConn(rows)
    view = sweeper.resolve_liveness(conn, pods, namespace="oddish-trials")
    assert view.owned_live_pods == frozenset()
    assert view.owned_dead_pods == frozenset()  # alias ignored, not dead-owned


def test_resolve_liveness_skips_handle_query_when_no_pods():
    conn = _FakeConn(rows=[], has_live_unlinked_worker=False)
    view = sweeper.resolve_liveness(conn, [], namespace="oddish-trials")
    # Only the running-handles guard query runs; the linkage query is skipped.
    assert len(conn.executed) == 1
    assert "RUNNING" in conn.executed[0][0]
    assert view.owned_live_pods == frozenset()
    assert view.owned_dead_pods == frozenset()


def test_unlinked_guard_classifies_gke_like_the_cluster_reaper():
    # The guard must recognize GKE trials by environment (out-of-process runs)
    # OR by the gke variant id (rows routed via the worker-side default
    # fallback, environment column unpopulated) -- one predicate alone leaves
    # a live unlinked pod deletable as an orphan.
    conn = _FakeConn(rows=[], has_live_unlinked_worker=False)
    sweeper.resolve_liveness(conn, [], namespace="oddish-trials")
    guard_sql = conn.executed[0][0]
    assert "t.environment = 'gke'" in guard_sql
    assert "t.harbor_config ->> 'variant_id' = 'gke'" in guard_sql


def test_run_sweep_deletes_only_orphans_and_lists_before_db_read():
    pods = [
        _FakePod("live", age=timedelta(hours=2)),
        _FakePod("orphan", age=timedelta(hours=2)),
        _FakePod("fresh", age=timedelta(minutes=1)),
    ]
    core = _FakeCoreV1(pods)
    conn = _FakeConn(rows=[("oddish-trials/live", "RUNNING", None)])
    plan = sweeper.run_sweep(
        core,
        conn,
        namespace="oddish-trials",
        grace_period=timedelta(minutes=10),
        now=NOW,
    )
    assert core.deleted == ["orphan"]
    assert set(plan.to_delete) == {"orphan"}
    # pods listed before any DB query (snapshot-then-resolve ordering)
    assert core.calls[0].startswith("list:")
    # grace_period_seconds=0 (immediate delete), matching handle-based teardown
    assert "delete:orphan:0" in core.calls
    # the delete is guarded by the snapshotted pod UID
    assert core.delete_bodies["orphan"]["preconditions"]["uid"] == "uid-orphan"


def test_delete_pod_uid_precondition_and_gone_statuses():
    # A replaced pod (same name, different instance) yields 412; an
    # already-gone pod yields 404. Both mean "the pod we meant is gone".
    core = _FakeCoreV1([], raise_status={"replaced": 412, "gone": 404})
    assert sweeper._delete_pod(core, "oddish-trials", "replaced", "uid-x") is True
    assert sweeper._delete_pod(core, "oddish-trials", "gone", "uid-y") is True
    # A genuine failure is reported (best-effort, not fatal).
    core2 = _FakeCoreV1([], raise_status={"boom": 500})
    assert sweeper._delete_pod(core2, "oddish-trials", "boom", "uid-z") is False
    # UID precondition present when a uid is given, absent otherwise.
    core3 = _FakeCoreV1([])
    sweeper._delete_pod(core3, "oddish-trials", "p", "uid-p")
    assert core3.delete_bodies["p"]["preconditions"]["uid"] == "uid-p"
    sweeper._delete_pod(core3, "oddish-trials", "q", None)
    assert core3.delete_bodies["q"] is None


def test_run_sweep_keeps_live_unlinked_pod_aliased_to_a_stale_terminal_handle():
    # End-to-end guard for the pod-name-reuse case: a live trial's pod (its own
    # handle not yet/ever recorded) whose name collides with an old terminal
    # trial's stale handle must NOT be deleted.
    pods = [_FakePod("P", age=timedelta(hours=2))]
    core = _FakeCoreV1(pods)
    conn = _FakeConn(
        # old terminal trial finished 3h ago; this pod was created 2h ago
        rows=[("oddish-trials/P", "SUCCESS", NOW - timedelta(hours=3))],
        has_live_unlinked_worker=True,  # the live trial B, handle not recorded
    )
    plan = sweeper.run_sweep(
        core,
        conn,
        namespace="oddish-trials",
        grace_period=timedelta(minutes=10),
        now=NOW,
    )
    assert core.deleted == []
    assert plan.to_delete == []


def test_run_sweep_dry_run_deletes_nothing():
    pods = [_FakePod("orphan", age=timedelta(hours=2))]
    core = _FakeCoreV1(pods)
    conn = _FakeConn(rows=[])
    plan = sweeper.run_sweep(
        core,
        conn,
        namespace="oddish-trials",
        grace_period=timedelta(minutes=10),
        now=NOW,
        dry_run=True,
    )
    assert core.deleted == []
    assert plan.to_delete == ["orphan"]


def test_run_sweep_is_best_effort_when_a_delete_fails():
    pods = [
        _FakePod("orphan1", age=timedelta(hours=2)),
        _FakePod("orphan2", age=timedelta(hours=2)),
    ]
    core = _FakeCoreV1(pods, fail_delete={"orphan1"})
    conn = _FakeConn(rows=[])
    sweeper.run_sweep(
        core,
        conn,
        namespace="oddish-trials",
        grace_period=timedelta(minutes=10),
        now=NOW,
    )
    # orphan1 raised, but the sweep continued and still deleted orphan2.
    assert core.deleted == ["orphan2"]


def test_run_sweep_empty_namespace_short_circuits():
    core = _FakeCoreV1([])
    conn = _FakeConn(rows=[])
    plan = sweeper.run_sweep(
        core,
        conn,
        namespace="oddish-trials",
        grace_period=timedelta(minutes=10),
        now=NOW,
    )
    assert plan.verdicts == ()
    assert conn.executed == []  # DB never touched when there are no pods


def test_dead_owner_temporal_check():
    p = _pod("x", age=timedelta(hours=1))  # created NOW-1h
    # finished after creation -> real owner
    assert sweeper._dead_owner_owns_pod(p, NOW) is True
    # finished before creation -> stale alias, not owner
    assert sweeper._dead_owner_owns_pod(p, NOW - timedelta(hours=2)) is False
    # missing finish (RETRYING/QUEUED) -> cannot disambiguate, match stands
    assert sweeper._dead_owner_owns_pod(p, None) is True


def test_handle_roundtrip_helpers():
    assert sweeper._pod_handle("oddish-trials", "p") == "oddish-trials/p"
    assert sweeper._pod_name_from_handle("oddish-trials/p") == "p"
    assert sweeper._pod_name_from_handle("no-slash") is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_stale_running_handle_activates_unlinked_guard():
    # After a retry, a RUNNING worker_job can still carry the PREVIOUS
    # attempt's handle. Its new pod is not yet linked -- the guard must treat
    # a RUNNING trial whose handle points at a NON-EXISTENT pod exactly like
    # one with no handle at all, or the new pod gets deleted as an orphan.
    pods = [_pod("attempt-two-pod", age=timedelta(minutes=20))]
    conn = _FakeConn(
        rows=[],  # no worker_job references the new pod yet
        running_gke_handles=["oddish-trials/attempt-one-pod-gone"],
    )
    view = sweeper.resolve_liveness(conn, pods, namespace="oddish-trials")
    assert view.has_live_unlinked_worker is True
    plan = sweeper.decide_orphan_pods(
        pods, view, now=NOW, grace_period=timedelta(minutes=10)
    )
    assert plan.to_delete == []


def test_running_handle_matching_existing_pod_does_not_trip_guard():
    # A RUNNING trial whose handle matches a CURRENT pod is simply linked;
    # it must not suppress orphan deletion of unrelated pods.
    pods = [
        _pod("linked-pod", age=timedelta(hours=1)),
        _pod("true-orphan", age=timedelta(hours=1)),
    ]
    conn = _FakeConn(
        rows=[("oddish-trials/linked-pod", "RUNNING", None)],
        running_gke_handles=["oddish-trials/linked-pod"],
    )
    view = sweeper.resolve_liveness(conn, pods, namespace="oddish-trials")
    assert view.has_live_unlinked_worker is False
    plan = sweeper.decide_orphan_pods(
        pods, view, now=NOW, grace_period=timedelta(minutes=10)
    )
    assert plan.to_delete == ["true-orphan"]


def test_dead_owner_compare_survives_naive_timestamps():
    from datetime import datetime, timezone

    pod = PodSnapshot(
        name="p",
        phase="Running",
        creation_time=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        labels={"app": "sandbox", "session": "p"},
    )
    naive_after = datetime(2026, 7, 9, 13, 0)  # naive, after creation
    naive_before = datetime(2026, 7, 9, 11, 0)  # naive, before creation
    assert sweeper._dead_owner_owns_pod(pod, naive_after) is True
    assert sweeper._dead_owner_owns_pod(pod, naive_before) is False
