"""Idle-cluster reaper: the delete half of zero-touch GKE elasticity.

Auto-provisioning creates the cluster on demand; this reaper deletes it once
the deployment has been quiet long enough, so an unused cluster never sits
around paying the Autopilot management fee. Recreation is automatic and free
(the next trial's ensure_cluster brings it back), which makes deletion safe
by construction. Three independent guards before any delete:

  1. Database quiet: no live GKE trials, and the newest GKE trial activity is
     older than the TTL (a deployment with no GKE trials at all is governed by
     the cluster's own creation age instead).
  2. Ownership: the cluster carries the harbor-managed resource label written
     by ensure_cluster -- hand-made clusters are never touched.
  3. Cluster empty: no pods remain in the trials namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from oddish.config import settings
from oddish.db import get_session
from oddish.db.models import TrialModel

_LIVE_STATUSES = ("queued", "running", "retrying")


@dataclass(frozen=True)
class ReapDecision:
    action: str  # "reap" | "skip"
    reason: str


def decide(
    *,
    now: datetime,
    ttl_hours: float,
    live_gke_trials: int,
    last_gke_activity: datetime | None,
    cluster_exists: bool,
    cluster_created_at: datetime | None,
    cluster_managed: bool,
    pods_in_namespace: int,
) -> ReapDecision:
    """Pure reap decision over the three guards (unit-testable)."""
    if ttl_hours <= 0:
        return ReapDecision("skip", "reaper disabled (ttl<=0)")
    if not cluster_exists:
        return ReapDecision("skip", "no cluster")
    if live_gke_trials > 0:
        return ReapDecision("skip", f"{live_gke_trials} live GKE trials")
    idle_since = last_gke_activity or cluster_created_at
    if idle_since is None:
        return ReapDecision("skip", "no activity or creation timestamp")
    idle = now - idle_since
    if idle < timedelta(hours=ttl_hours):
        return ReapDecision("skip", f"idle {idle} < ttl {ttl_hours}h")
    if not cluster_managed:
        return ReapDecision("skip", "cluster lacks the harbor-managed label")
    if pods_in_namespace > 0:
        return ReapDecision("skip", f"{pods_in_namespace} pods still present")
    return ReapDecision("reap", f"idle {idle} >= ttl {ttl_hours}h")


def _parse_cluster_created(value) -> datetime | None:
    """Cluster create_time is an RFC3339 STRING in container_v1; accept a
    datetime too in case a future client returns one. Naive values are UTC."""
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _gke_trial_activity() -> tuple[int, datetime | None]:
    async with get_session() as session:
        # Environment is the routing truth: harbor-gke pins at non-blessed
        # SHAs classify as the "ephemeral" variant yet still run on GKE, so
        # counting by variant alone would let the reaper delete a cluster
        # mid-run. The variant predicate stays for legacy rows without the
        # environment column populated.
        variant_is_gke = or_(
            TrialModel.environment == "gke",
            TrialModel.harbor_config["variant_id"].astext == "gke",
        )
        live = await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(variant_is_gke, TrialModel.status.in_(_LIVE_STATUSES))
        )
        last = await session.scalar(
            select(func.max(TrialModel.updated_at)).where(variant_is_gke)
        )
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return int(live or 0), last


async def reap_idle_cluster(deploy_app_name: str | None = None) -> str:
    """Run one reap evaluation; returns the decision for logs."""
    import asyncio

    ttl = settings.gke_idle_cluster_ttl_hours
    cluster_name = settings.gke_cluster_name
    if not (cluster_name and settings.gke_region and settings.gke_project_id):
        return "skip: GKE not configured"
    # Same ownership rule as teardown, checked before any cloud call: an
    # idle, managed, SHARED cluster this deployment was merely pointed at
    # must not be reaped either -- idleness here is judged from this
    # deployment's own database, which knows nothing about the other users.
    if not teardown_owns_cluster(cluster_name, _expected_app_name(deploy_app_name)):
        return "skip: cluster name is not this deployment's derived name"
    if ttl <= 0:
        return "skip: reaper disabled (ttl<=0)"

    from worker.runtime import _materialize_gcp_adc_credentials

    _materialize_gcp_adc_credentials()

    import google.auth
    from google.api_core import exceptions as gcp_exceptions
    from google.cloud import container_v1

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    manager = container_v1.ClusterManagerClient(credentials=creds)
    name = (
        f"projects/{settings.gke_project_id}/locations/{settings.gke_region}"
        f"/clusters/{cluster_name}"
    )
    try:
        cluster = await asyncio.to_thread(manager.get_cluster, name=name)
    except gcp_exceptions.NotFound:
        cluster = None

    cluster_managed = False
    created_at: datetime | None = None
    if cluster is not None:
        cluster_managed = (
            dict(cluster.resource_labels or {}).get("harbor-managed") == "true"
        )
        created_at = _parse_cluster_created(cluster.create_time)

    live, last_activity = await _gke_trial_activity()
    now = datetime.now(timezone.utc)

    # Phase 1: every cheap guard, assuming an empty cluster. Anything that
    # skips here never touches the Kubernetes API at all -- unmanaged
    # clusters, live deployments, and young clusters cost one get_cluster.
    pre = decide(
        now=now,
        ttl_hours=ttl,
        live_gke_trials=live,
        last_gke_activity=last_activity,
        cluster_exists=cluster is not None,
        cluster_created_at=created_at,
        cluster_managed=cluster_managed,
        pods_in_namespace=0,
    )
    if pre.action != "reap":
        return f"skip: {pre.reason}"

    # Phase 2: the one side-effectful probe, attempted for ANY existing
    # cluster state (RECONCILING/DEGRADED can still host pods). A failed
    # listing means pods are UNKNOWN -- fail safe and skip.
    pods_count = await _probe_pods()
    if pods_count is None:
        return "skip: pod listing failed; refusing to reap blind"

    # Refresh the DB view after the probe so the verdict sees trials that
    # went live meanwhile. The residual instant between this read and the
    # delete is retry-covered: a racing trial's attempt fails against the
    # deleting cluster and its retry re-provisions.
    live, last_activity = await _gke_trial_activity()
    decision = decide(
        now=now,
        ttl_hours=ttl,
        live_gke_trials=live,
        last_gke_activity=last_activity,
        cluster_exists=True,
        cluster_created_at=created_at,
        cluster_managed=cluster_managed,
        pods_in_namespace=pods_count,
    )
    if decision.action == "reap":
        await asyncio.to_thread(manager.delete_cluster, name=name)
        return f"reaped {cluster_name}: {decision.reason}"
    return f"skip: {decision.reason}"


async def _probe_pods() -> int | None:
    """Count pods in the trials namespace; None when the probe fails."""
    import asyncio

    try:
        from harbor.environments.gke_auth import build_core_api

        api = await asyncio.to_thread(
            build_core_api,
            settings.gke_cluster_name,
            settings.gke_region,
            settings.gke_project_id,
        )
        pods = await asyncio.to_thread(
            api.list_namespaced_pod, namespace=settings.gke_namespace
        )
        return len(pods.items)
    except Exception:  # noqa: BLE001 -- unknown pods must block reaping
        return None


# Bounded wait for a delete to finish. Long enough for the ordinary case,
# short enough not to stall a redeploy pipeline; whatever it does not cover
# is retry-covered on the trial side.
_TEARDOWN_WAIT_SEC = 240.0
_TEARDOWN_POLL_SEC = 10.0


def select_teardown_targets(
    clusters: "list[tuple[str, dict[str, str], str]]", cluster_name: str
) -> list[str]:
    """Pick which clusters a deployment teardown may delete.

    ``clusters`` is ``(name, resource_labels, full_resource_path)`` for every
    cluster the project can see, across all locations -- a region override on
    a submission can have provisioned the deployment's cluster somewhere the
    settings never mention, so teardown must search wider than
    ``settings.gke_region``.

    Two guards, both absolute: the name must be exactly this deployment's
    cluster name, and the cluster must carry ``harbor-managed: "true"``. A
    hand-built cluster that happens to share the name is never touched.
    """
    return [
        path
        for name, labels, path in clusters
        if name == cluster_name and labels.get("harbor-managed") == "true"
    ]


def teardown_owns_cluster(cluster_name: str | None, app_name: str) -> bool:
    """Whether deletion may touch this cluster name at all.

    Only the app-derived name -- what auto-provisioning would create for
    THIS deployment -- qualifies. A name that is not the derived one is a
    cluster the deployment was pointed at, not one it owns: a preview aimed
    at a shared cluster must not delete it, and the managed label cannot
    make that distinction because every provisioned cluster carries it. A
    configured name that EQUALS the derived one is fine; it names the same
    resource either way.
    """
    return bool(cluster_name) and cluster_name == f"{app_name}-trials"


def _expected_app_name(deploy_app_name: str | None) -> str:
    """The deployment identity deletion authorizes against.

    Callers registered as Modal functions bind this at DEPLOY time as a
    pickled default argument, because the runtime secret layer can overwrite
    MODAL_APP_NAME in the container environment -- and an identity that env
    can change is not an identity. The env fallback exists for direct calls
    and tests only.
    """
    import os

    return deploy_app_name or os.environ.get("MODAL_APP_NAME", "oddish")


async def teardown_deployment_cluster(deploy_app_name: str | None = None) -> str:
    """Delete this deployment's auto-provisioned cluster(s), wherever they are.

    The scheduled idle reaper dies with the Modal app, so a closing preview
    must delete its cluster while the app still exists to do it -- this is
    the function the stop workflow calls just before ``modal app stop``.
    Deletion is awaited, bounded. The pinned harbor raises when it finds a
    cluster in STOPPING during provisioning, so a trial racing a redeploy
    would burn attempts against a half-deleted cluster until the delete
    completes. Waiting here shrinks that window to nothing in the common
    case; if the cap expires, the residual window is retry-covered, because
    the provisioning error is an ordinary retryable failure inside the
    trial run.
    """
    import asyncio

    cluster_name = settings.gke_cluster_name
    if not (cluster_name and settings.gke_project_id):
        return "skip: GKE not configured"
    if not teardown_owns_cluster(cluster_name, _expected_app_name(deploy_app_name)):
        return "skip: cluster name is not this deployment's derived name"

    from worker.runtime import _materialize_gcp_adc_credentials

    _materialize_gcp_adc_credentials()

    import google.auth
    from google.api_core import exceptions as gcp_exceptions
    from google.cloud import container_v1

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    manager = container_v1.ClusterManagerClient(credentials=creds)
    parent = f"projects/{settings.gke_project_id}/locations/-"
    listing = await asyncio.to_thread(manager.list_clusters, parent=parent)
    seen = [
        (
            c.name,
            dict(c.resource_labels or {}),
            f"projects/{settings.gke_project_id}/locations/{c.location}"
            f"/clusters/{c.name}",
        )
        for c in listing.clusters
    ]
    targets = select_teardown_targets(seen, cluster_name)
    if not targets:
        matched = [name for name, _, _ in seen if name == cluster_name]
        if matched:
            return "skip: cluster exists but is not harbor-managed"
        return "skip: no cluster"

    deleted = []
    for path in targets:
        try:
            await asyncio.to_thread(manager.delete_cluster, name=path)
            deleted.append(path)
        except gcp_exceptions.NotFound:
            pass

    gone = 0
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _TEARDOWN_WAIT_SEC
    for path in deleted:
        while True:
            try:
                await asyncio.to_thread(manager.get_cluster, name=path)
            except gcp_exceptions.NotFound:
                gone += 1
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(_TEARDOWN_POLL_SEC)

    if gone == len(deleted):
        return f"deleted {gone} cluster(s)"
    return f"deleting {len(deleted)} cluster(s) ({gone} gone, rest in progress)"
