"""Deploy-time GKE cluster-existence gate (modal_app.assert_gke_cluster_exists).

The platform never creates clusters; workers only connect to the standing
cluster named by ODDISH_GKE_CLUSTER_NAME. These tests pin the gate's contract:
definitive not-found blocks the deploy, everything else never does.
"""

import subprocess

import pytest

import modal_app


def _configure(monkeypatch, **overrides):
    cfg = {
        "ODDISH_GKE_CLUSTER_NAME": "oddish-tpu-use5",
        "ODDISH_GKE_REGION": "us-east5",
        "ODDISH_GKE_PROJECT_ID": "abundant-default",
        # The gate only hard-checks when zero-touch provisioning is off.
        "ODDISH_GKE_AUTO_PROVISION_CLUSTER": "false",
    }
    cfg.update(overrides)
    for key, value in cfg.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(modal_app, "LOCAL_DOTENV_VARS", {})


def test_no_gke_config_is_a_noop(monkeypatch):
    _configure(
        monkeypatch,
        ODDISH_GKE_CLUSTER_NAME=None,
        ODDISH_GKE_REGION=None,
        ODDISH_GKE_PROJECT_ID=None,
    )

    def must_not_run(*args, **kwargs):
        raise AssertionError("subprocess must not run without GKE config")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    modal_app.assert_gke_cluster_exists()


def test_missing_gcloud_skips_without_blocking(monkeypatch):
    _configure(monkeypatch)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    modal_app.assert_gke_cluster_exists()


def test_definitive_not_found_blocks_deploy(monkeypatch):
    _configure(monkeypatch)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gcloud")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr=(
                "ERROR: (gcloud.container.clusters.describe) ResponseError: "
                "code=404, message=Not found: projects/p/locations/r/clusters/c."
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="not found in region 'us-east5'"):
        modal_app.assert_gke_cluster_exists()


def test_transient_failure_does_not_block_deploy(monkeypatch, capsys):
    _configure(monkeypatch)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gcloud")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="ERROR: gcloud crashed: network unreachable",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    modal_app.assert_gke_cluster_exists()
    assert "could not verify" in capsys.readouterr().out


def test_timeout_does_not_block_deploy(monkeypatch, capsys):
    _configure(monkeypatch)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gcloud")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gcloud", timeout=45)

    monkeypatch.setattr(subprocess, "run", fake_run)
    modal_app.assert_gke_cluster_exists()
    assert "timed out" in capsys.readouterr().out


def test_existing_cluster_passes(monkeypatch, capsys):
    _configure(monkeypatch)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gcloud")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="oddish-tpu-use5\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    modal_app.assert_gke_cluster_exists()
    assert "verified" in capsys.readouterr().out


def test_auto_provision_skips_the_existence_gate(monkeypatch, capsys):
    _configure(monkeypatch, ODDISH_GKE_AUTO_PROVISION_CLUSTER="true")
    import shutil

    def must_not_run(_):
        raise AssertionError("gcloud must not run when auto-provision is on")

    monkeypatch.setattr(shutil, "which", must_not_run)
    modal_app.assert_gke_cluster_exists()
    assert "auto-provisioned on demand" in capsys.readouterr().out
