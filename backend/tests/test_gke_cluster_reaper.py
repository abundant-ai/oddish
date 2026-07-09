"""Idle-cluster reap decision matrix (worker/gke_cluster_reaper.decide)."""

from datetime import datetime, timedelta, timezone

import pytest

from worker.gke_cluster_reaper import decide

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def _decide(**overrides):
    base = dict(
        now=NOW,
        ttl_hours=3.0,
        live_gke_trials=0,
        last_gke_activity=NOW - timedelta(hours=5),
        cluster_exists=True,
        cluster_created_at=NOW - timedelta(days=1),
        cluster_managed=True,
        pods_in_namespace=0,
    )
    base.update(overrides)
    return decide(**base)


def test_idle_managed_empty_cluster_is_reaped():
    d = _decide()
    assert d.action == "reap"


def test_live_trials_block_reaping():
    assert _decide(live_gke_trials=2).action == "skip"


def test_recent_activity_blocks_reaping():
    d = _decide(last_gke_activity=NOW - timedelta(minutes=30))
    assert d.action == "skip"


def test_unmanaged_cluster_is_never_touched():
    d = _decide(cluster_managed=False)
    assert d.action == "skip"
    assert "harbor-managed" in d.reason


def test_pods_present_block_reaping():
    assert _decide(pods_in_namespace=1).action == "skip"


def test_disabled_ttl_skips():
    assert _decide(ttl_hours=0).action == "skip"


def test_missing_cluster_skips():
    assert _decide(cluster_exists=False).action == "skip"


def test_never_used_cluster_reaped_by_creation_age():
    d = _decide(last_gke_activity=None, cluster_created_at=NOW - timedelta(hours=4))
    assert d.action == "reap"


def test_never_used_young_cluster_kept():
    d = _decide(last_gke_activity=None, cluster_created_at=NOW - timedelta(minutes=20))
    assert d.action == "skip"


def test_no_timestamps_at_all_skips():
    d = _decide(last_gke_activity=None, cluster_created_at=None)
    assert d.action == "skip"


async def test_orchestration_skips_blind_when_pod_probe_fails(monkeypatch):
    import worker.gke_cluster_reaper as reaper

    monkeypatch.setattr(reaper.settings, "gke_cluster_name", "c")
    monkeypatch.setattr(reaper.settings, "gke_region", "r")
    monkeypatch.setattr(reaper.settings, "gke_project_id", "p")
    monkeypatch.setattr(reaper.settings, "gke_idle_cluster_ttl_hours", 1.0)

    async def no_activity():
        return 0, NOW - timedelta(hours=9)

    monkeypatch.setattr(reaper, "_gke_trial_activity", no_activity)

    async def probe_fails():
        return None

    monkeypatch.setattr(reaper, "_probe_pods", probe_fails)
    monkeypatch.setattr("worker.runtime._materialize_gcp_adc_credentials", lambda: None)

    import sys
    from types import SimpleNamespace

    import google.auth
    import google.cloud

    monkeypatch.setattr(google.auth, "default", lambda scopes: (object(), "p"))

    fake_cluster = SimpleNamespace(
        resource_labels={"harbor-managed": "true"},
        create_time="2026-07-09T00:00:00+00:00",
        status=SimpleNamespace(name="RECONCILING"),
    )
    fake_mgr = SimpleNamespace(
        get_cluster=lambda name: fake_cluster,
        delete_cluster=lambda name: (_ for _ in ()).throw(
            AssertionError("must not delete when pods are unknown")
        ),
    )
    # The lean test venv has no google-cloud-container; the reaper imports it
    # lazily, so satisfy the import seam with a stand-in module.
    fake_container = SimpleNamespace(ClusterManagerClient=lambda credentials: fake_mgr)
    monkeypatch.setitem(sys.modules, "google.cloud.container_v1", fake_container)
    monkeypatch.setattr(google.cloud, "container_v1", fake_container, raising=False)
    out = await reaper.reap_idle_cluster()
    assert "refusing to reap blind" in out


def test_parse_cluster_created_accepts_string_datetime_and_junk():
    from worker.gke_cluster_reaper import _parse_cluster_created

    s = _parse_cluster_created("2026-07-09T15:30:00Z")
    assert s is not None and s.tzinfo is not None
    d = _parse_cluster_created(datetime(2026, 7, 9, 15, 30))
    assert d is not None and d.tzinfo is not None  # naive -> UTC
    assert _parse_cluster_created("") is None
    assert _parse_cluster_created(None) is None
