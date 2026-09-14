"""Container placement rides along as a resource attribute."""

from __future__ import annotations

from observability import _extra_resource_attributes


def test_modal_placement_becomes_resource_attributes(monkeypatch):
    monkeypatch.setenv("MODAL_REGION", "us-east")
    monkeypatch.setenv("MODAL_CLOUD_PROVIDER", "aws")
    monkeypatch.delenv("MODAL_APP_NAME", raising=False)

    attrs = _extra_resource_attributes()

    assert attrs["oddish.modal_region"] == "us-east"
    assert attrs["oddish.modal_cloud"] == "aws"


def test_missing_placement_adds_nothing(monkeypatch):
    monkeypatch.delenv("MODAL_REGION", raising=False)
    monkeypatch.delenv("MODAL_CLOUD_PROVIDER", raising=False)

    attrs = _extra_resource_attributes()

    assert "oddish.modal_region" not in attrs
    assert "oddish.modal_cloud" not in attrs
