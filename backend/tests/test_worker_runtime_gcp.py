"""The worker materializes the oddish-gcp secret's inline JSON into a file so
Google client libraries (which only read GOOGLE_APPLICATION_CREDENTIALS as a
path) can discover ADC on the GKE backend.

The deploy-side counterpart -- attaching that oddish-gcp secret to worker
containers whenever GKE is configured -- is covered at the bottom of this file.
"""

from __future__ import annotations

import os

import modal_app
import worker.runtime as runtime


def test_materializes_credentials_when_json_present(tmp_path, monkeypatch) -> None:
    target = tmp_path / "gcp-sa.json"
    monkeypatch.setattr(runtime, "_GCP_ADC_CREDENTIALS_PATH", str(target))
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"type": "service_account"}'
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    runtime._materialize_gcp_adc_credentials()

    assert target.read_text() == '{"type": "service_account"}'
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(target)


def test_noop_when_json_absent(tmp_path, monkeypatch) -> None:
    target = tmp_path / "gcp-sa.json"
    monkeypatch.setattr(runtime, "_GCP_ADC_CREDENTIALS_PATH", str(target))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    runtime._materialize_gcp_adc_credentials()

    assert not target.exists()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_existing_credentials_file_not_rewritten(tmp_path, monkeypatch) -> None:
    # Modal reuses containers; once written the file is left in place so the
    # per-invocation setup stays cheap.
    target = tmp_path / "gcp-sa.json"
    target.write_text("original")
    monkeypatch.setattr(runtime, "_GCP_ADC_CREDENTIALS_PATH", str(target))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "replacement")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    runtime._materialize_gcp_adc_credentials()

    assert target.read_text() == "original"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(target)


# Deploy-side gate: the oddish-gcp secret must ride along on worker containers
# whenever GKE is configured -- and GKE is configured by whichever channel also
# drives backend registration (oddish.runtime.registry), i.e. an explicit
# process env var OR backend/.env (LOCAL_DOTENV_VARS, injected as a from_dict
# secret at runtime). These pin the gate condition to that same precedence.


def test_gke_secret_gate_reads_process_env() -> None:
    assert (
        modal_app._effective_gke_cluster_name(
            {"ODDISH_GKE_CLUSTER_NAME": "oddish-tpu"}, {}
        )
        == "oddish-tpu"
    )


def test_gke_secret_gate_reads_dotenv() -> None:
    # Regression: GKE set only in backend/.env still attaches the secret. That
    # dotenv value reaches the worker and registers the GKE backend, so the
    # deploy-time gate has to see it too or GKE trials boot without ADC.
    assert (
        modal_app._effective_gke_cluster_name(
            {}, {"ODDISH_GKE_CLUSTER_NAME": "oddish-tpu"}
        )
        == "oddish-tpu"
    )


def test_gke_secret_gate_absent_when_unconfigured() -> None:
    # A GKE-less deploy references no oddish-gcp secret and still boots.
    assert modal_app._effective_gke_cluster_name({}, {}) is None


def test_gke_secret_gate_process_env_takes_precedence() -> None:
    # Same precedence pydantic-settings uses: an explicit env var wins over .env.
    assert (
        modal_app._effective_gke_cluster_name(
            {"ODDISH_GKE_CLUSTER_NAME": "from-env"},
            {"ODDISH_GKE_CLUSTER_NAME": "from-dotenv"},
        )
        == "from-env"
    )
