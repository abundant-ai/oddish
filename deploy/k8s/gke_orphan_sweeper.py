"""Backstop reaper for orphaned GKE trial sandbox pods.

Trial pods (label ``app=sandbox``) in the ``oddish-trials`` namespace are
normally torn down by Harbor's own ``finally`` on the variant image (normal
completion) and by Oddish's handle-based teardown via ``(provider,
external_id)`` when a worker is cancelled/deleted. Two cases slip past both:

  * a worker that never persisted the sandbox handle (the ENVIRONMENT_START
    DB write failed, or the trial's slow startup was stale-reaped mid-flight)
    leaves a pod with no ``external_id`` on any worker_job — invisible to the
    handle-based teardown; and
  * the periodic reconciler now runs on a Kubernetes-less image and cannot do
    pod deletes at all.

Either way a pod can outlive its trial. A Running orphan's cost is bounded by
the pod's ``activeDeadlineSeconds`` (24h), but a *Pending* orphan is not (k8s
does not count Pending time against that deadline) and it pins a flex-start
ProvisioningRequest against the per-project/region ``ACTIVE_RESIZE_REQUESTS``
cap, so orphans can starve capacity. This CronJob closes both holes.

Safety is the whole point: it must NEVER delete a pod that belongs to a live
trial — including a trial whose pod is legitimately Pending while it waits for
DWS capacity (those waits are long and indeterminate, so pod age alone can
never condemn a Pending pod). Ownership is therefore decided against the
database, not the clock: a pod is deletable only when no ``RUNNING``
worker_job owns it. See :func:`decide_orphan_pods` for the exact rule and the
guard that covers the (rare) live-but-unlinked case.

The decision logic (:func:`decide_orphan_pods` and its inputs) is pure and
lives here so it can be unit-tested without a cluster or a database. The
runner (:func:`run_sweep` / :func:`main`) adds the Kubernetes + read-only
Postgres glue and is intentionally dependency-light: it imports only
``kubernetes`` and ``psycopg`` (both lazily), so its image does not carry the
Oddish worker runtime or ``harbor[gke]`` and keeps working even when Oddish's
own dispatch is wedged.

Deployment (see the sibling manifests in this directory):

  * ``kubectl apply -f deploy/k8s/orphan-sweeper-rbac.yaml`` — a ServiceAccount
    + Role granting ``list``/``delete`` on pods in ``oddish-trials`` only.
  * a read-only Postgres secret the CronJob mounts as ``ODDISH_DATABASE_URL``
    (a role with SELECT on ``worker_jobs`` and ``trials`` is sufficient; the
    connection is additionally pinned read-only at runtime).
  * ``kubectl apply -f deploy/k8s/orphan-sweeper-cronjob.yaml``.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("gke_orphan_sweeper")

# The label Harbor stamps on every trial sandbox pod (single-container and DinD
# alike). The sweeper only ever considers pods carrying it.
SANDBOX_APP_LABEL = "sandbox"

# Liveness is read straight from the queue's own scheduling state: a worker_job
# in ``RUNNING`` is live. We deliberately do NOT second-guess it with heartbeat
# freshness — a worker can be alive but have its heartbeat writes stalled by a
# pooler outage for many minutes (the queue folds those failures rather than
# killing the trial), and a long TPU/DWS Pending wait can outlast any heartbeat
# threshold. The queue's stale-reaper is the sole authority on death: it moves a
# genuinely-dead worker_job out of ``RUNNING`` (a database-only transition that
# still runs on the Kubernetes-less image). Only once it has done so does this
# sweeper consider the pod reapable. Trusting ``RUNNING`` is strictly the safer
# side: at worst a dead worker's pod lingers until the reaper transitions it
# (its cost bounded by the pod's 24h activeDeadlineSeconds).

# A pod younger than this is never touched, regardless of ownership. It covers
# the brief window between a pod being created and its owning worker_job
# becoming observable (handle persisted), and absorbs clock skew between the
# Kubernetes API and the database.
DEFAULT_GRACE_PERIOD = timedelta(minutes=10)

DEFAULT_NAMESPACE = "oddish-trials"


class Verdict(str, enum.Enum):
    """Why the sweeper decided to keep or delete a pod (drives the log)."""

    # Deleted: no worker_job references this pod and no live worker could own
    # it (the pre-hook / lost-handle orphan).
    DELETE_ORPHAN = "delete_orphan"
    # Deleted: a worker_job references this pod but that job is no longer RUNNING
    # (terminal, or reaped out of RUNNING) — the lost-teardown orphan.
    DELETE_DEAD_OWNER = "delete_dead_owner"
    # Kept: a RUNNING worker_job owns this pod.
    KEEP_LIVE = "keep_live"
    # Kept: too young to judge.
    KEEP_WITHIN_GRACE = "keep_within_grace"
    # Kept: no worker_job references this pod, but a live worker_job with no
    # persisted handle exists, so this pod *might* be its (as-yet-unlinked)
    # sandbox. Conservative: never risk a live trial's pod.
    KEEP_LIVE_UNLINKED_GUARD = "keep_live_unlinked_guard"
    # Kept: not a trial sandbox pod (defensive; the lister already filters).
    KEEP_NOT_SANDBOX = "keep_not_sandbox"


_DELETE_VERDICTS = frozenset({Verdict.DELETE_ORPHAN, Verdict.DELETE_DEAD_OWNER})


@dataclasses.dataclass(frozen=True)
class PodSnapshot:
    """The bits of a pod the decision needs. ``creation_time`` is tz-aware.

    ``uid`` is the pod's immutable Kubernetes identity, captured at list time and
    used as a delete precondition so a same-named pod created after the snapshot
    (Kubernetes names are reusable) can never be deleted in its place.
    """

    name: str
    phase: str
    creation_time: datetime | None
    labels: dict[str, str]
    uid: str | None = None


@dataclasses.dataclass(frozen=True)
class LivenessView:
    """Ownership of the *currently existing* pods, resolved from worker_jobs.

    ``owned_live_pods`` / ``owned_dead_pods`` are pod names (not full handles)
    reached from a worker_job's ``external_id``; a name is in ``owned_live`` iff
    a ``RUNNING`` worker_job carries its handle, and in ``owned_dead`` iff a
    non-``RUNNING`` worker_job carries its handle *and* could have created this
    pod (it did not finish before the pod was created — otherwise the handle is
    a stale alias for a since-reused pod name and is ignored). Live always wins.
    ``has_live_unlinked_worker`` is true iff some ``RUNNING`` GKE trial
    worker_job has no ``external_id`` yet — a live trial whose sandbox we cannot
    name, so an unreferenced pod might be its.
    """

    owned_live_pods: frozenset[str]
    owned_dead_pods: frozenset[str]
    has_live_unlinked_worker: bool


@dataclasses.dataclass(frozen=True)
class PodVerdict:
    name: str
    verdict: Verdict


@dataclasses.dataclass(frozen=True)
class SweepPlan:
    verdicts: tuple[PodVerdict, ...]

    @property
    def to_delete(self) -> list[str]:
        return [v.name for v in self.verdicts if v.verdict in _DELETE_VERDICTS]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {v.value: 0 for v in Verdict}
        for pv in self.verdicts:
            out[pv.verdict.value] += 1
        return out


def _verdict_for(
    pod: PodSnapshot,
    liveness: LivenessView,
    *,
    now: datetime,
    grace_period: timedelta,
) -> Verdict:
    if pod.labels.get("app") != SANDBOX_APP_LABEL:
        return Verdict.KEEP_NOT_SANDBOX

    # A live (RUNNING) owner wins over every other signal — including a
    # terminal row that happens to share the name and the grace check — so a
    # pod that might still be in use is never deleted.
    if pod.name in liveness.owned_live_pods:
        return Verdict.KEEP_LIVE

    # Can't establish the pod is old enough to judge → keep. A missing
    # creation timestamp is treated as "too young", never as "ancient".
    if pod.creation_time is None or now - pod.creation_time <= grace_period:
        return Verdict.KEEP_WITHIN_GRACE

    # Past grace and owned by a worker_job the queue no longer runs (terminal,
    # or reaped out of RUNNING): the trial is gone but the pod (and its
    # ProvisioningRequest) lingered — the lost-teardown orphan.
    if pod.name in liveness.owned_dead_pods:
        return Verdict.DELETE_DEAD_OWNER

    # Past grace and unreferenced by any worker_job. Normally a true orphan,
    # but if a live worker_job has not yet recorded its handle this pod might
    # be that (still-unlinked) live sandbox — so we only delete when no such
    # worker exists.
    if liveness.has_live_unlinked_worker:
        return Verdict.KEEP_LIVE_UNLINKED_GUARD
    return Verdict.DELETE_ORPHAN


def decide_orphan_pods(
    pods: Sequence[PodSnapshot],
    liveness: LivenessView,
    *,
    now: datetime,
    grace_period: timedelta,
) -> SweepPlan:
    """Decide, per pod, whether it is a deletable orphan.

    Pure and side-effect free: given the current pods, the ownership view
    resolved from ``worker_jobs``, the reference time and the grace window, it
    returns a verdict for every pod. The runner deletes exactly the pods whose
    verdict is in :data:`_DELETE_VERDICTS`.

    The invariant that matters: a pod is only ever condemned when the database
    shows no live owner for it *and* no live worker could plausibly own it
    unrecorded. Age never condemns on its own.
    """
    verdicts = tuple(
        PodVerdict(
            name=pod.name,
            verdict=_verdict_for(pod, liveness, now=now, grace_period=grace_period),
        )
        for pod in pods
    )
    return SweepPlan(verdicts=verdicts)


# ---------------------------------------------------------------------------
# Runner: Kubernetes + read-only Postgres glue.
#
# Everything below imports ``kubernetes`` / ``psycopg`` lazily, so importing
# the module for the pure decision logic (tests) needs neither. Objects from
# those libraries are typed ``Any`` on purpose.
# ---------------------------------------------------------------------------

from typing import Any  # noqa: E402


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pod_handle(namespace: str, pod_name: str) -> str:
    """The ``external_id`` a worker_job records for this pod (``ns/pod``)."""
    return f"{namespace}/{pod_name}"


def _pod_name_from_handle(handle: str) -> str | None:
    ns, sep, name = handle.partition("/")
    return name if sep and name else None


def list_sandbox_pods(core_v1: Any, namespace: str) -> list[PodSnapshot]:
    """Snapshot every trial sandbox pod (``app=sandbox``) in *namespace*."""
    resp = core_v1.list_namespaced_pod(
        namespace=namespace, label_selector=f"app={SANDBOX_APP_LABEL}"
    )
    pods: list[PodSnapshot] = []
    for item in resp.items:
        meta = item.metadata
        status = item.status
        pods.append(
            PodSnapshot(
                name=meta.name,
                phase=(status.phase if status and status.phase else "Unknown"),
                creation_time=meta.creation_timestamp,
                labels=dict(meta.labels or {}),
                uid=meta.uid,
            )
        )
    return pods


def _dead_owner_owns_pod(pod: PodSnapshot, finished_at: datetime | None) -> bool:
    """Would a non-RUNNING worker_job that finished at *finished_at* really own
    this pod?

    A real owner records its handle and creates the pod while RUNNING, so the
    pod is always created before the job finishes. If instead the job finished
    strictly before the pod was created, the handle is a stale alias for a pod
    name that has since been reused by a newer trial — treat it as not-owned so
    we never condemn the newer (possibly live) pod on a dead trial's account.
    A missing ``finished_at`` (e.g. a RETRYING/QUEUED row still carrying a prior
    attempt's handle) or missing pod creation time cannot be disambiguated, so
    the match stands.
    """
    if finished_at is None or pod.creation_time is None:
        return True
    # timestamptz columns and Kubernetes timestamps are both tz-aware in
    # practice, but a naive value from either side must degrade to a wrong-ish
    # answer, never a TypeError that aborts the whole sweep.
    finished = (
        finished_at.replace(tzinfo=timezone.utc)
        if finished_at.tzinfo is None
        else finished_at
    )
    created = (
        pod.creation_time.replace(tzinfo=timezone.utc)
        if pod.creation_time.tzinfo is None
        else pod.creation_time
    )
    return finished >= created


def resolve_liveness(
    conn: Any,
    pods: Sequence[PodSnapshot],
    *,
    namespace: str,
) -> LivenessView:
    """Resolve ownership of the given pods from ``worker_jobs``.

    A pod is *owned-live* iff a ``TRIAL`` worker_job carries its handle
    (``external_id == "<namespace>/<pod>"``) and is ``RUNNING``. It is
    *owned-dead* iff a non-``RUNNING`` worker_job carries its handle and could
    have created it (see :func:`_dead_owner_owns_pod`). Live always wins.

    ``has_live_unlinked_worker`` is decided separately: a ``RUNNING``
    GKE-environment trial that has not yet recorded a handle -- or whose
    recorded handle points at a pod that no longer exists (a retry's stale
    handle from a previous attempt) -- could own an as-yet-unlinked pod. It is keyed off ``trials.environment = 'gke'`` (not the
    variant id) so it also covers a GKE trial that ran on an out-of-process
    Harbor.
    """
    by_handle = {_pod_handle(namespace, p.name): p for p in pods}
    handles = list(by_handle)

    live: set[str] = set()
    dead: set[str] = set()
    if handles:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_id, status, finished_at
                FROM worker_jobs
                WHERE kind = 'TRIAL' AND external_id = ANY(%(handles)s)
                """,
                {"handles": handles},
            )
            for external_id, status, finished_at in cur.fetchall():
                pod = by_handle.get(external_id or "")
                if pod is None:
                    continue
                if status == "RUNNING":
                    live.add(pod.name)
                elif _dead_owner_owns_pod(pod, finished_at):
                    dead.add(pod.name)
    dead -= live

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT wj.external_id
            FROM worker_jobs wj
            JOIN trials t
              ON t.id = wj.subject_id AND wj.subject_table = 'trials'
            WHERE wj.kind = 'TRIAL'
              AND wj.status = 'RUNNING'
              AND t.environment = 'gke'
            """
        )
        running_handles = [r[0] for r in cur.fetchall()]
    # A RUNNING trial with NO handle -- or with a handle pointing at a pod that
    # no longer exists (a retry can carry the PREVIOUS attempt's handle until
    # the new attempt's hook write lands) -- may own one of the unreferenced
    # pods as its not-yet-linked sandbox. Both cases must arm the guard, or a
    # retried trial's fresh pod gets deleted as an orphan.
    has_live_unlinked_worker = any(
        handle is None or handle not in by_handle for handle in running_handles
    )

    return LivenessView(
        owned_live_pods=frozenset(live),
        owned_dead_pods=frozenset(dead),
        has_live_unlinked_worker=has_live_unlinked_worker,
    )


def _delete_pod(core_v1: Any, namespace: str, name: str, uid: str | None) -> bool:
    """Delete one pod immediately, guarded by its UID.

    Passing the UID as a delete precondition means that if the pod we snapshotted
    was already deleted and a *different* pod was created with the same name, the
    API server refuses with 412 and we do not delete the impostor. 404 (already
    gone) and 412 (replaced) both mean "the pod we meant is gone" → nothing to do.

    Best-effort: any other failure is logged and swallowed so one bad pod cannot
    abort the sweep. Status codes are duck-typed off the ``status`` a kubernetes
    ``ApiException`` carries, and the delete body is a plain dict, so this needs
    no kubernetes import.
    """
    body = None
    if uid:
        body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": uid},
        }
    try:
        core_v1.delete_namespaced_pod(
            name=name, namespace=namespace, grace_period_seconds=0, body=body
        )
        return True
    except Exception as exc:
        if getattr(exc, "status", None) in (404, 412):
            return True
        logger.warning("failed to delete pod %s: %s", name, exc)
        return False


def run_sweep(
    core_v1: Any,
    conn: Any,
    *,
    namespace: str,
    grace_period: timedelta,
    dry_run: bool = False,
    now: datetime | None = None,
) -> SweepPlan:
    """List pods, resolve liveness, decide, delete orphans, log a summary.

    Pods are listed BEFORE the database is read so that any pod newer than the
    snapshot cannot be judged: a pod present in the snapshot was created before
    ``now``, and its handle (written before the pod exists) is therefore
    already visible to the subsequent database read.
    """
    pods = list_sandbox_pods(core_v1, namespace)
    if now is None:
        now = _utcnow()
    if not pods:
        logger.info("metric=gke_orphan_sweep namespace=%s counted=0", namespace)
        return SweepPlan(verdicts=())

    liveness = resolve_liveness(conn, pods, namespace=namespace)
    plan = decide_orphan_pods(pods, liveness, now=now, grace_period=grace_period)

    uid_by_name = {p.name: p.uid for p in pods}
    deleted = 0
    errors = 0
    for name in plan.to_delete:
        if dry_run:
            logger.info("dry-run: would delete orphan pod %s", name)
            continue
        if _delete_pod(core_v1, namespace, name, uid_by_name.get(name)):
            deleted += 1
        else:
            errors += 1

    counts = plan.counts()
    logger.info(
        "metric=gke_orphan_sweep namespace=%s counted=%d deleted=%d errors=%d "
        "dry_run=%s live=%d dead_owner=%d orphan=%d within_grace=%d "
        "live_unlinked_guard=%d not_sandbox=%d has_live_unlinked_worker=%s",
        namespace,
        len(pods),
        deleted,
        errors,
        dry_run,
        counts[Verdict.KEEP_LIVE.value],
        counts[Verdict.DELETE_DEAD_OWNER.value],
        counts[Verdict.DELETE_ORPHAN.value],
        counts[Verdict.KEEP_WITHIN_GRACE.value],
        counts[Verdict.KEEP_LIVE_UNLINKED_GUARD.value],
        counts[Verdict.KEEP_NOT_SANDBOX.value],
        liveness.has_live_unlinked_worker,
    )
    return plan


def _env_minutes(name: str, default: timedelta) -> timedelta:
    raw = os.environ.get(name)
    if not raw:
        return default
    return timedelta(minutes=float(raw))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    database_url = os.environ.get("ODDISH_DATABASE_URL")
    if not database_url:
        logger.error("ODDISH_DATABASE_URL is required")
        return 2

    namespace = os.environ.get("ODDISH_GKE_NAMESPACE", DEFAULT_NAMESPACE)
    grace_period = _env_minutes("ODDISH_SWEEP_GRACE_MINUTES", DEFAULT_GRACE_PERIOD)
    dry_run = _env_flag("ODDISH_SWEEP_DRY_RUN")

    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    core_v1 = client.CoreV1Api()

    import psycopg

    # Pin the connection read-only server-side so the sweeper cannot mutate the
    # database even if handed an over-privileged role.
    with psycopg.connect(
        database_url,
        autocommit=True,
        options="-c default_transaction_read_only=on",
    ) as conn:
        run_sweep(
            core_v1,
            conn,
            namespace=namespace,
            grace_period=grace_period,
            dry_run=dry_run,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
