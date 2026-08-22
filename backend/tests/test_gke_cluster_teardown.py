"""Deployment teardown may delete only its own, harbor-managed cluster."""

from worker.gke_cluster_reaper import select_teardown_targets


def _cluster(name, managed, location="loc-a"):
    labels = {"harbor-managed": "true"} if managed else {}
    return (name, labels, f"projects/p/locations/{location}/clusters/{name}")


def test_the_deployments_managed_cluster_is_selected():
    targets = select_teardown_targets([_cluster("dep-trials", True)], "dep-trials")
    assert targets == ["projects/p/locations/loc-a/clusters/dep-trials"]


def test_every_location_is_covered():
    """A region override on a submission can provision the cluster somewhere
    the deployment settings never name, so teardown searches all locations."""
    clusters = [
        _cluster("dep-trials", True, "loc-a"),
        _cluster("dep-trials", True, "loc-b"),
    ]
    assert len(select_teardown_targets(clusters, "dep-trials")) == 2


def test_an_unmanaged_cluster_is_never_touched():
    assert select_teardown_targets([_cluster("dep-trials", False)], "dep-trials") == []


def test_another_deployments_cluster_is_never_touched():
    assert select_teardown_targets([_cluster("other-trials", True)], "dep-trials") == []


def test_a_managed_label_with_the_wrong_value_does_not_count():
    cluster = ("dep-trials", {"harbor-managed": "yes"}, "projects/p/locations/l/clusters/dep-trials")
    assert select_teardown_targets([cluster], "dep-trials") == []
