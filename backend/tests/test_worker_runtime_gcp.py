"""The worker materializes the oddish-gcp secret's inline JSON into a file so
Google client libraries (which only read GOOGLE_APPLICATION_CREDENTIALS as a
path) can discover ADC on the GKE backend.
"""

from __future__ import annotations

import os

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
